# Lead Finder — Web App

A small internal web UI over `find_leads.py`'s search/filter/score pipeline,
for non-technical sales/marketing use. FastAPI backend, plain HTML/CSS/JS
frontend (no build step).

## Setup

From the `lead_finder/` directory (the parent of this `webapp/` folder):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r webapp/requirements.txt
```

If you already created `.venv` and installed `requirements.txt` for the CLI
tool, you only need to add the webapp's extra dependencies:

```bash
source .venv/bin/activate
pip install -r webapp/requirements.txt
```

## Configure

Create `webapp/.env` (this file is gitignored — never commit it):

```
GOOGLE_PLACES_API_KEY=your_google_places_api_key
WEBAPP_USER=your_chosen_username
WEBAPP_PASSWORD=your_chosen_password
RATE_LIMIT_PER_HOUR=20
```

- `GOOGLE_PLACES_API_KEY` — same key the CLI tool uses. Loaded server-side
  only; it is never sent to the browser or accepted as a request parameter.
- `WEBAPP_USER` / `WEBAPP_PASSWORD` — the single HTTP Basic Auth credential
  pair for this tool. Anyone with these can run searches (which cost Google
  API calls), so treat them like a password, not a public link.
- `RATE_LIMIT_PER_HOUR` — optional, defaults to 20. Max `/api/search` calls
  per user per rolling hour.

## Run

```bash
cd webapp
source ../.venv/bin/activate   # if not already active
uvicorn main:app --reload --port 8000
```

Then open **http://localhost:8000** in a browser. The first request to any
`/api/*` endpoint will trigger the browser's native Basic Auth prompt — enter
`WEBAPP_USER` / `WEBAPP_PASSWORD` there (the browser caches it for the rest
of the session).

## What it stores

- `webapp/leads.db` — SQLite database of every search run through the web
  app (params, timestamp, and the full result set as JSON), powering the
  History tab. Gitignored; safe to delete to reset history.
- Search results and Place Details are also cached by `find_leads.py`'s own
  cache at `lead_finder/places_cache.json` (shared with the CLI tool), so
  repeating the same city/category within 30 days won't re-hit the Google
  API even across a rate-limit reset.

## Running tests

```bash
cd webapp
python -m unittest test_api.py -v
```

Tests mock the Google Places client — they never call the live API.
