#!/usr/bin/env python3
"""Lead finder for To The Stars Ratings — surfaces small local businesses
with weak review presence using the Google Places API."""

import argparse
import csv
import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import googlemaps
from googlemaps.exceptions import ApiError, HTTPError, Timeout, TransportError

# ---------------------------------------------------------------------------
# Config — edit these to change what gets searched by default.
# ---------------------------------------------------------------------------

CITY_CATEGORY_PAIRS = [
    ("Salt Lake City, UT", "hair salon"),
    ("Salt Lake City, UT", "auto repair"),
    ("Salt Lake City, UT", "nail salon"),
    ("Salt Lake City, UT", "landscaping"),
    ("Salt Lake City, UT", "hvac contractor"),
]

CACHE_FILE = Path(__file__).parent / "places_cache.json"
CACHE_TTL_DAYS = 30

OUTPUT_FILE = Path(__file__).parent / "leads.csv"

# Google allows ~10 QPS by default; stay well under it.
REQUESTS_PER_SECOND = 5
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 1.5

# Scoring thresholds — passed into score_lead() as the `thresholds` dict, so
# a future UI or config file can override them without touching the scoring
# logic itself.
DEFAULT_SCORING_THRESHOLDS = {
    "high_review_count_max": 15,
    "high_rating_min": 3.3,
    "high_rating_max": 4.2,
    "medium_review_min": 15,
    "medium_review_max": 40,
    "low_review_count_min": 100,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("lead_finder")


# ---------------------------------------------------------------------------
# Local cache — keyed by "city|category" for searches, place_id for details.
# ---------------------------------------------------------------------------

class Cache:
    def __init__(self, path: Path):
        self.path = path
        self.data = {"searches": {}, "details": {}}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
                self.data.setdefault("searches", {})
                self.data.setdefault("details", {})
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Could not read cache file (%s), starting fresh", e)

    def save(self):
        self.path.write_text(json.dumps(self.data, indent=2))

    @staticmethod
    def _search_key(city: str, category: str) -> str:
        return f"{city.strip().lower()}|{category.strip().lower()}"

    def get_search(self, city: str, category: str, refresh: bool):
        if refresh:
            return None
        entry = self.data["searches"].get(self._search_key(city, category))
        if entry is None:
            return None
        age_days = (time.time() - entry["timestamp"]) / 86400
        if age_days > CACHE_TTL_DAYS:
            return None
        return entry["results"]

    def set_search(self, city: str, category: str, results: list):
        self.data["searches"][self._search_key(city, category)] = {
            "timestamp": time.time(),
            "results": results,
        }

    def get_details(self, place_id: str, refresh: bool):
        if refresh:
            return None
        return self.data["details"].get(place_id)

    def set_details(self, place_id: str, details: dict):
        self.data["details"][place_id] = details


# ---------------------------------------------------------------------------
# Rate limiting / backoff
# ---------------------------------------------------------------------------

class RateLimiter:
    def __init__(self, requests_per_second: float):
        self.min_interval = 1.0 / requests_per_second
        self._last_call = 0.0

    def wait(self):
        elapsed = time.monotonic() - self._last_call
        remaining = self.min_interval - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_call = time.monotonic()


def call_with_backoff(rate_limiter: RateLimiter, func, *args, **kwargs):
    """Calls func with rate limiting plus exponential backoff on transient errors."""
    for attempt in range(1, MAX_RETRIES + 1):
        rate_limiter.wait()
        try:
            return func(*args, **kwargs)
        except (Timeout, TransportError, HTTPError) as e:
            if attempt == MAX_RETRIES:
                raise
            delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            log.warning("Transient error (%s), retrying in %.1fs [%d/%d]", e, delay, attempt, MAX_RETRIES)
            time.sleep(delay)
        except ApiError as e:
            if e.status == "OVER_QUERY_LIMIT" and attempt < MAX_RETRIES:
                delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                log.warning("Over query limit, retrying in %.1fs [%d/%d]", delay, attempt, MAX_RETRIES)
                time.sleep(delay)
                continue
            raise
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------------------
# Places API
# ---------------------------------------------------------------------------

def search_places(gmaps, rate_limiter, cache: Cache, city: str, category: str, refresh: bool) -> list:
    cached = cache.get_search(city, category, refresh)
    if cached is not None:
        log.info("Using cached results for %r in %r (%d places)", category, city, len(cached))
        return cached

    query = f"{category} in {city}"
    log.info("Searching Places API: %r", query)

    all_results = []
    response = call_with_backoff(rate_limiter, gmaps.places, query=query)
    all_results.extend(response.get("results", []))

    while "next_page_token" in response:
        # Google requires a short delay before a next_page_token becomes valid.
        time.sleep(2.5)
        token = response["next_page_token"]
        response = call_with_backoff(rate_limiter, gmaps.places, query=query, page_token=token)
        all_results.extend(response.get("results", []))

    log.info("Found %d results for %r in %r", len(all_results), category, city)
    cache.set_search(city, category, all_results)
    cache.save()
    return all_results


def get_place_details(gmaps, rate_limiter, cache: Cache, place_id: str, refresh: bool) -> dict:
    cached = cache.get_details(place_id, refresh)
    if cached is not None:
        return cached

    response = call_with_backoff(
        rate_limiter,
        gmaps.place,
        place_id=place_id,
        fields=["formatted_phone_number", "website"],
    )
    result = response.get("result", {})
    details = {
        "phone": result.get("formatted_phone_number", ""),
        "website": result.get("website", ""),
    }
    cache.set_details(place_id, details)
    return details


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_lead(rating, review_count: int, has_website: bool, thresholds: dict = DEFAULT_SCORING_THRESHOLDS):
    """Returns (score, reasoning). Order of checks matters — HIGH is
    evaluated first, then LOW's catch-alls, then the MEDIUM band. Businesses
    that fall outside every explicit band (e.g. 50 reviews, has a website,
    4.7 rating) default to MEDIUM since they're neither clearly a strong
    lead nor clearly saturated."""

    rating_in_sweet_spot = (
        rating is not None
        and thresholds["high_rating_min"] <= rating <= thresholds["high_rating_max"]
    )
    few_reviews = review_count < thresholds["high_review_count_max"]

    if has_website and (few_reviews or rating_in_sweet_spot):
        reasons = []
        if few_reviews:
            reasons.append(f"only {review_count} review{'s' if review_count != 1 else ''}")
        if rating_in_sweet_spot:
            reasons.append(f"{rating}★ rating has room to improve")
        reasons.append("has a website (actively marketing)")
        return "HIGH", "; ".join(reasons)

    if not has_website:
        return "LOW", "no website found"

    if review_count > thresholds["low_review_count_min"]:
        return "LOW", f"{review_count} reviews — already well-established"

    if thresholds["medium_review_min"] <= review_count <= thresholds["medium_review_max"]:
        return "MEDIUM", f"{review_count} reviews — moderate review presence"

    rating_str = f"{rating}★" if rating is not None else "no rating"
    return "MEDIUM", f"{review_count} reviews, {rating_str} — doesn't clearly fit HIGH or LOW criteria"


SCORE_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


# ---------------------------------------------------------------------------
# Result filters — applied before scoring runs, so callers (CLI today, a web
# UI later) can pull businesses by review count / rating / website presence
# regardless of what score they'd end up with.
# ---------------------------------------------------------------------------

@dataclass
class LeadFilters:
    min_reviews: Optional[int] = None
    max_reviews: Optional[int] = None
    min_rating: Optional[float] = None
    max_rating: Optional[float] = None
    has_website: Optional[bool] = None  # None = don't filter on this


def _passes_review_and_rating_filters(rating, review_count: int, filters: Optional[LeadFilters]) -> bool:
    if filters is None:
        return True
    if filters.min_reviews is not None and review_count < filters.min_reviews:
        return False
    if filters.max_reviews is not None and review_count > filters.max_reviews:
        return False
    if filters.min_rating is not None and (rating is None or rating < filters.min_rating):
        return False
    if filters.max_rating is not None and (rating is None or rating > filters.max_rating):
        return False
    return True


def _passes_website_filter(has_website: bool, filters: Optional[LeadFilters]) -> bool:
    if filters is None or filters.has_website is None:
        return True
    return has_website == filters.has_website


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def build_leads(gmaps, cache: Cache, pairs: list, refresh: bool, filters: Optional[LeadFilters] = None) -> list:
    rate_limiter = RateLimiter(REQUESTS_PER_SECOND)
    leads = []

    for city, category in pairs:
        places = search_places(gmaps, rate_limiter, cache, city, category, refresh)

        for place in places:
            rating = place.get("rating")
            review_count = place.get("user_ratings_total", 0)

            # Check review-count/rating filters before spending a Place
            # Details call (and before scoring) on a place we'd drop anyway.
            if not _passes_review_and_rating_filters(rating, review_count, filters):
                continue

            place_id = place.get("place_id")
            details = get_place_details(gmaps, rate_limiter, cache, place_id, refresh)
            cache.save()

            website = details.get("website", "")
            has_website = bool(website)

            if not _passes_website_filter(has_website, filters):
                continue

            score, reasoning = score_lead(rating, review_count, has_website)

            leads.append({
                "business_name": place.get("name", ""),
                "category": category,
                "city": city,
                "address": place.get("formatted_address", ""),
                "phone": details.get("phone", ""),
                "website": website,
                "rating": rating,
                "review_count": review_count,
                "score": score,
                "reasoning": reasoning,
                "place_id": place_id,
            })

    leads.sort(key=lambda lead: (SCORE_ORDER[lead["score"]], lead["review_count"]))
    return leads


def write_csv(leads: list, output_path: Path):
    fieldnames = [
        "business_name", "category", "city", "address", "phone", "website",
        "rating", "review_count", "score", "reasoning", "place_id",
    ]
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(leads)
    log.info("Wrote %d leads to %s", len(leads), output_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", help="Run a single one-off search for this city (requires --category)")
    parser.add_argument("--category", help="Run a single one-off search for this category (requires --city)")
    parser.add_argument("--output", default=str(OUTPUT_FILE), help="Path to write the ranked CSV")
    parser.add_argument("--cache-file", default=str(CACHE_FILE), help="Path to the local cache file")
    parser.add_argument("--refresh", action="store_true", help="Ignore cache and re-query the Places API")

    parser.add_argument("--min-reviews", type=int, default=None, help="Only include businesses with at least this many reviews")
    parser.add_argument("--max-reviews", type=int, default=None, help="Only include businesses with at most this many reviews")
    parser.add_argument("--min-rating", type=float, default=None, help="Only include businesses rated at least this high")
    parser.add_argument("--max-rating", type=float, default=None, help="Only include businesses rated at most this high")
    parser.add_argument("--has-website", dest="has_website", action="store_true", default=None, help="Only include businesses that have a website")
    parser.add_argument("--no-website-filter", dest="has_website", action="store_false", help="Only include businesses with no website")

    return parser.parse_args()


def main():
    args = parse_args()

    if bool(args.city) != bool(args.category):
        print("Error: --city and --category must be provided together for a one-off run.", file=sys.stderr)
        sys.exit(1)

    if args.min_reviews is not None and args.max_reviews is not None and args.min_reviews > args.max_reviews:
        print("Error: --min-reviews cannot be greater than --max-reviews.", file=sys.stderr)
        sys.exit(1)

    if args.min_rating is not None and args.max_rating is not None and args.min_rating > args.max_rating:
        print("Error: --min-rating cannot be greater than --max-rating.", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not api_key:
        print("Error: set the GOOGLE_PLACES_API_KEY environment variable.", file=sys.stderr)
        sys.exit(1)

    pairs = [(args.city, args.category)] if args.city else CITY_CATEGORY_PAIRS

    filters = LeadFilters(
        min_reviews=args.min_reviews,
        max_reviews=args.max_reviews,
        min_rating=args.min_rating,
        max_rating=args.max_rating,
        has_website=args.has_website,
    )

    gmaps = googlemaps.Client(key=api_key)
    cache = Cache(Path(args.cache_file))

    leads = build_leads(gmaps, cache, pairs, args.refresh, filters=filters)
    write_csv(leads, Path(args.output))

    high = sum(1 for lead in leads if lead["score"] == "HIGH")
    medium = sum(1 for lead in leads if lead["score"] == "MEDIUM")
    low = sum(1 for lead in leads if lead["score"] == "LOW")
    log.info("Scored %d leads — HIGH: %d, MEDIUM: %d, LOW: %d", len(leads), high, medium, low)


if __name__ == "__main__":
    main()
