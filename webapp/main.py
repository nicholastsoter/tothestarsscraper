"""FastAPI web layer for the lead finder. Wraps find_leads.py's existing
search/filter/score pipeline — no scoring or Places-API logic lives here."""

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

WEBAPP_DIR = Path(__file__).resolve().parent
LEAD_FINDER_DIR = WEBAPP_DIR.parent
sys.path.insert(0, str(LEAD_FINDER_DIR))
sys.path.insert(0, str(WEBAPP_DIR))

load_dotenv(WEBAPP_DIR / ".env")

import googlemaps  # noqa: E402
from fastapi import Depends, FastAPI, HTTPException  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import find_leads  # noqa: E402
from auth import require_auth  # noqa: E402
from db import SearchStore  # noqa: E402
from models import HistoryDetail, HistoryEntry, Lead, SearchRequest, SearchResponse  # noqa: E402

app = FastAPI(title="To The Stars Ratings — Lead Finder")

DB_PATH = Path(os.environ.get("LEADS_DB_PATH", WEBAPP_DIR / "leads.db"))
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
def health(username: str = Depends(require_auth)):
    return {"status": "ok"}


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

    try:
        leads = find_leads.build_leads(
            gmaps, cache, [(request.city, request.category)], refresh=False, filters=filters
        )
    except (
        googlemaps.exceptions.ApiError,
        googlemaps.exceptions.HTTPError,
        googlemaps.exceptions.Timeout,
        googlemaps.exceptions.TransportError,
    ) as e:
        raise HTTPException(status_code=502, detail=f"Google Places API error: {e}")

    search_id = store.record_search(username, request.model_dump(), leads)

    return SearchResponse(leads=[Lead(**lead) for lead in leads], search_id=search_id)


@app.get("/api/history", response_model=list[HistoryEntry])
def history(limit: int = 50, username: str = Depends(require_auth)):
    return store.list_history(limit=limit)


@app.get("/api/history/{search_id}", response_model=HistoryDetail)
def history_detail(search_id: int, username: str = Depends(require_auth)):
    entry = store.get_search(search_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Search not found")
    return entry


app.mount("/", StaticFiles(directory=str(WEBAPP_DIR / "static"), html=True), name="static")
