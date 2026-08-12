import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from find_leads import Cache, LeadFilters, RateLimiter, build_leads, write_csv


class FakeGmaps:
    """Stands in for googlemaps.Client — no network calls."""

    def __init__(self):
        self.places_calls = 0
        self.place_calls = 0

    def places(self, query, page_token=None):
        self.places_calls += 1
        if page_token:
            return {"results": [self._place("Overflow Salon", 3.9, 8)]}
        return {
            "results": [
                self._place("Tiny Salon", 3.9, 5),
                self._place("Big Chain Salon", 4.5, 500),
                self._place("Mid Salon", 4.6, 25),
            ],
            "next_page_token": "TOKEN1",
        }

    def place(self, place_id, fields):
        self.place_calls += 1
        # "Big Chain Salon" has no website -> should be forced LOW.
        if place_id == "place_Big Chain Salon":
            return {"result": {"formatted_phone_number": "555-2222"}}
        return {
            "result": {
                "formatted_phone_number": "555-1111",
                "website": f"https://example.com/{place_id}",
            }
        }

    @staticmethod
    def _place(name, rating, review_count):
        return {
            "place_id": f"place_{name}",
            "name": name,
            "formatted_address": "123 Main St, Salt Lake City, UT",
            "rating": rating,
            "user_ratings_total": review_count,
        }


class PipelineTests(unittest.TestCase):
    def test_build_leads_and_cache_and_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cache.json"
            csv_path = Path(tmp) / "leads.csv"

            gmaps = FakeGmaps()
            cache = Cache(cache_path)
            pairs = [("Salt Lake City, UT", "hair salon")]

            leads = build_leads(gmaps, cache, pairs, refresh=False)

            # 3 results page 1 + 1 result page 2 (pagination followed)
            self.assertEqual(len(leads), 4)
            self.assertEqual(gmaps.places_calls, 2)
            self.assertEqual(gmaps.place_calls, 4)

            by_name = {lead["business_name"]: lead for lead in leads}
            self.assertEqual(by_name["Tiny Salon"]["score"], "HIGH")
            self.assertEqual(by_name["Big Chain Salon"]["score"], "LOW")
            self.assertEqual(by_name["Big Chain Salon"]["website"], "")

            # Sorted HIGH -> MEDIUM -> LOW
            scores_in_order = [lead["score"] for lead in leads]
            self.assertEqual(scores_in_order, sorted(scores_in_order, key=lambda s: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}[s]))

            write_csv(leads, csv_path)
            with csv_path.open() as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[0]["business_name"], "Tiny Salon")

            # Second run should hit the cache instead of calling the API again.
            gmaps2 = FakeGmaps()
            cache2 = Cache(cache_path)
            leads2 = build_leads(gmaps2, cache2, pairs, refresh=False)
            self.assertEqual(gmaps2.places_calls, 0)
            self.assertEqual(gmaps2.place_calls, 0)
            self.assertEqual(len(leads2), 4)

            # --refresh should bypass the cache and re-query.
            gmaps3 = FakeGmaps()
            cache3 = Cache(cache_path)
            build_leads(gmaps3, cache3, pairs, refresh=True)
            self.assertEqual(gmaps3.places_calls, 2)


class FilterPipelineTests(unittest.TestCase):
    """FakeGmaps places, for reference:
    Tiny Salon (3.9★, 5 reviews, website), Big Chain Salon (4.5★, 500 reviews,
    NO website), Mid Salon (4.6★, 25 reviews, website), Overflow Salon
    (3.9★, 8 reviews, website, from page 2)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self._tmp.name) / "cache.json"
        self.csv_path = Path(self._tmp.name) / "leads.csv"
        self.pairs = [("Salt Lake City, UT", "hair salon")]

    def tearDown(self):
        self._tmp.cleanup()

    def _build(self, filters):
        gmaps = FakeGmaps()
        cache = Cache(self.cache_path)
        leads = build_leads(gmaps, cache, self.pairs, refresh=False, filters=filters)
        return leads, gmaps

    def test_min_reviews_filter(self):
        leads, gmaps = self._build(LeadFilters(min_reviews=10))
        names = {lead["business_name"] for lead in leads}
        self.assertEqual(names, {"Big Chain Salon", "Mid Salon"})
        # Review-count filtering happens before the Place Details call, so
        # places dropped for too few reviews shouldn't cost a details call.
        self.assertEqual(gmaps.place_calls, 2)

    def test_max_reviews_filter(self):
        leads, _ = self._build(LeadFilters(max_reviews=30))
        names = {lead["business_name"] for lead in leads}
        self.assertEqual(names, {"Tiny Salon", "Mid Salon", "Overflow Salon"})

    def test_min_rating_filter(self):
        leads, _ = self._build(LeadFilters(min_rating=4.0))
        names = {lead["business_name"] for lead in leads}
        self.assertEqual(names, {"Big Chain Salon", "Mid Salon"})

    def test_max_rating_filter(self):
        leads, _ = self._build(LeadFilters(max_rating=4.0))
        names = {lead["business_name"] for lead in leads}
        self.assertEqual(names, {"Tiny Salon", "Overflow Salon"})

    def test_has_website_true_filter(self):
        leads, _ = self._build(LeadFilters(has_website=True))
        names = {lead["business_name"] for lead in leads}
        self.assertEqual(names, {"Tiny Salon", "Mid Salon", "Overflow Salon"})

    def test_has_website_false_filter(self):
        leads, _ = self._build(LeadFilters(has_website=False))
        names = {lead["business_name"] for lead in leads}
        self.assertEqual(names, {"Big Chain Salon"})

    def test_combined_filters(self):
        leads, _ = self._build(LeadFilters(min_reviews=10, max_rating=4.6, has_website=True))
        names = {lead["business_name"] for lead in leads}
        self.assertEqual(names, {"Mid Salon"})

    def test_filters_excluding_everything_writes_empty_csv_with_headers(self):
        leads, gmaps = self._build(LeadFilters(min_reviews=1000))
        self.assertEqual(leads, [])
        # No place cleared the review-count filter, so no details calls fired.
        self.assertEqual(gmaps.place_calls, 0)

        write_csv(leads, self.csv_path)

        with self.csv_path.open() as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].split(","), [
            "business_name", "category", "city", "address", "phone", "website",
            "rating", "review_count", "score", "reasoning", "place_id",
        ])

        with self.csv_path.open() as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows, [])


class RateLimiterTests(unittest.TestCase):
    def test_enforces_minimum_interval(self):
        import time
        limiter = RateLimiter(requests_per_second=20)  # 50ms interval
        start = time.monotonic()
        for _ in range(3):
            limiter.wait()
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.09)  # ~2 intervals of 50ms


if __name__ == "__main__":
    unittest.main()
