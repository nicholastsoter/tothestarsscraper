"""FastAPI web layer for the lead finder. Wraps find_leads.py's existing
search/filter/score pipeline — no scoring or Places-API logic lives here."""

import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

WEBAPP_DIR = Path(__file__).resolve().parent
LEAD_FINDER_DIR = WEBAPP_DIR.parent
sys.path.insert(0, str(LEAD_FINDER_DIR))
sys.path.insert(0, str(WEBAPP_DIR))

load_dotenv(WEBAPP_DIR / ".env")

import googlemaps  # noqa: E402
import requests  # noqa: E402
from fastapi import Depends, FastAPI, HTTPException  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import find_leads  # noqa: E402
from auth import require_auth  # noqa: E402
from db import PostgresSearchStore, SearchStore  # noqa: E402
from models import (  # noqa: E402
    ContactedListEntry,
    ContactedRequest,
    ContactedResponse,
    ContactedStats,
    HistoryDetail,
    HistoryEntry,
    Lead,
    SearchRequest,
    SearchResponse,
    TemplateContent,
    TemplateResponse,
)

CONTACTED_DETAIL_FIELDS = (
    "business_name", "address", "phone", "website", "email",
    "city", "category", "rating", "review_count", "score",
)

TEMPLATE_KEYS = ("call_script", "email_template")

app = FastAPI(title="To The Stars Ratings — Lead Finder")

# Vercel's Python runtime filesystem is read-only outside /tmp, and /tmp
# itself isn't shared or persisted across invocations/cold starts — so a
# local SQLite file there loses everything on the next cold start (or even
# the next request, if it lands on a different instance). If a Postgres
# connection string is configured (e.g. via Vercel's Postgres/Neon
# integration), use that for real persistence instead; otherwise fall back
# to local SQLite, which is what local dev and the test suite use.
IS_VERCEL = bool(os.environ.get("VERCEL"))
DATABASE_URL = os.environ.get("POSTGRES_URL") or os.environ.get("DATABASE_URL")

if IS_VERCEL:
    find_leads.CACHE_FILE = Path("/tmp/places_cache.json")

if DATABASE_URL:
    store = PostgresSearchStore(DATABASE_URL)
else:
    DEFAULT_DB_PATH = Path("/tmp/leads.db") if IS_VERCEL else WEBAPP_DIR / "leads.db"
    DB_PATH = Path(os.environ.get("LEADS_DB_PATH", DEFAULT_DB_PATH))
    store = SearchStore(DB_PATH)

RATE_LIMIT_PER_HOUR = int(os.environ.get("RATE_LIMIT_PER_HOUR", "20"))


def get_gmaps_client() -> googlemaps.Client:
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="Server is missing GOOGLE_PLACES_API_KEY")
    try:
        return googlemaps.Client(key=api_key)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=f"Server has an invalid GOOGLE_PLACES_API_KEY: {e}")


def get_cache() -> find_leads.Cache:
    return find_leads.Cache(find_leads.CACHE_FILE)


def get_email_fetch():
    return requests.get


def _start_of_today_epoch() -> float:
    now = datetime.now()
    return datetime(now.year, now.month, now.day).timestamp()


def _start_of_week_epoch() -> float:
    """Start of the current ISO week (Monday 00:00, server-local time)."""
    now = datetime.now()
    start_of_today = datetime(now.year, now.month, now.day)
    return (start_of_today - timedelta(days=start_of_today.weekday())).timestamp()


def _attach_contacted_status(leads: list) -> list:
    """Merges in current "contacted" state from its own table rather than
    trusting whatever's in a stored search's frozen results_json — contacted
    status can change well after a search was run or recorded."""
    contacted_map = store.get_contacted_map([lead["place_id"] for lead in leads])
    for lead in leads:
        status = contacted_map.get(lead["place_id"], {})
        lead["contacted"] = status.get("contacted", False)
        lead["contacted_at"] = status.get("contacted_at")
    return leads


def enforce_rate_limit(username: str) -> None:
    since = time.time() - 3600
    recent = store.count_recent_searches(username, since)
    if recent >= RATE_LIMIT_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Rate limit exceeded: max {RATE_LIMIT_PER_HOUR} searches per hour. "
                "Try again later."
            ),
        )


@app.get("/api/health")
def health():
    # "storage" reports which backend is active — not the connection string
    # itself — so a persistence problem in production can be diagnosed from
    # this endpoint alone, without dashboard access to check env vars.
    return {"status": "ok", "storage": "postgres" if DATABASE_URL else "sqlite"}


@app.post("/api/search", response_model=SearchResponse)
def search(request: SearchRequest, username: str = Depends(require_auth)):
    # Validation and the rate-limit check must happen before we construct a
    # Places client or touch the network — Depends() parameters resolve
    # before this function body runs, so gmaps/cache are fetched manually
    # below rather than declared as Depends(), to keep this ordering.
    if request.min_reviews is not None and request.max_reviews is not None and request.min_reviews > request.max_reviews:
        raise HTTPException(status_code=400, detail="min_reviews cannot be greater than max_reviews")

    if request.min_rating is not None and request.max_rating is not None and request.min_rating > request.max_rating:
        raise HTTPException(status_code=400, detail="min_rating cannot be greater than max_rating")

    enforce_rate_limit(username)

    filters = find_leads.LeadFilters(
        min_reviews=request.min_reviews,
        max_reviews=request.max_reviews,
        min_rating=request.min_rating,
        max_rating=request.max_rating,
        has_website=request.has_website,
    )

    gmaps = get_gmaps_client()
    cache = get_cache()
    email_fetch = get_email_fetch()
    call_stats = {}

    try:
        leads = find_leads.build_leads(
            gmaps,
            cache,
            [(request.city, request.category)],
            refresh=False,
            filters=filters,
            email_fetch=email_fetch,
            call_stats=call_stats,
        )
    except (
        googlemaps.exceptions.ApiError,
        googlemaps.exceptions.HTTPError,
        googlemaps.exceptions.Timeout,
        googlemaps.exceptions.TransportError,
    ) as e:
        raise HTTPException(status_code=502, detail=f"Google Places API error: {e}")

    was_live_call = call_stats.get("live_api_call", False)
    search_id = store.record_search(username, request.model_dump(), leads, was_live_call=was_live_call)
    leads = _attach_contacted_status(leads)

    return SearchResponse(leads=[Lead(**lead) for lead in leads], search_id=search_id)


@app.get("/api/history", response_model=list[HistoryEntry])
def history(limit: int = 50, username: str = Depends(require_auth)):
    return store.list_history(limit=limit)


@app.get("/api/history/{search_id}", response_model=HistoryDetail)
def history_detail(search_id: int, username: str = Depends(require_auth)):
    entry = store.get_search(search_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Search not found")
    entry["results"] = _attach_contacted_status(entry["results"])
    return entry


@app.post("/api/contacted", response_model=ContactedResponse)
def set_contacted(request: ContactedRequest, username: str = Depends(require_auth)):
    details = {field: getattr(request, field) for field in CONTACTED_DETAIL_FIELDS}
    result = store.set_contacted(request.place_id, request.contacted, username, details=details)
    return ContactedResponse(**result)


@app.get("/api/contacted/stats", response_model=ContactedStats)
def contacted_stats(username: str = Depends(require_auth)):
    today = store.count_contacted_since(_start_of_today_epoch())
    this_week = store.count_contacted_since(_start_of_week_epoch())
    return ContactedStats(today=today, this_week=this_week)


@app.get("/api/contacted/list", response_model=list[ContactedListEntry])
def contacted_list(username: str = Depends(require_auth)):
    return store.list_contacted()


@app.get("/api/templates", response_model=dict[str, TemplateResponse])
def get_templates(username: str = Depends(require_auth)):
    templates = store.get_templates(list(TEMPLATE_KEYS))
    return {key: TemplateResponse(key=key, **value) for key, value in templates.items()}


@app.put("/api/templates/{key}", response_model=TemplateResponse)
def set_template(key: str, request: TemplateContent, username: str = Depends(require_auth)):
    if key not in TEMPLATE_KEYS:
        raise HTTPException(status_code=404, detail=f"Unknown template key: {key}")
    result = store.set_template(key, request.content, username)
    return TemplateResponse(**result)


app.mount("/", StaticFiles(directory=str(WEBAPP_DIR / "static"), html=True), name="static")
