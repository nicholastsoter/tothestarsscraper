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

Then open **http://localhost:8000** in a browser. You'll see a small sign-in
form — enter `WEBAPP_USER` / `WEBAPP_PASSWORD` there (kept in
`sessionStorage` for the tab, not a cookie or server session).

## What it stores

- Every search run through the web app (params, timestamp, full result
  set), the Contacted list, and the Call Script / Email Template text all
  live in one database — see "Storage backend" below for where that
  actually is.
- Search results and Place Details are also cached by `find_leads.py`'s own
  cache at `lead_finder/places_cache.json` (shared with the CLI tool), so
  repeating the same city/category within 30 days won't re-hit the Google
  API even across a rate-limit reset. This cache is separate from the
  database above and is fine to lose — it just means occasional extra
  Google API calls, not lost data.

## Storage backend

- **Locally (no `DATABASE_URL`/`POSTGRES_URL` set):** a SQLite file at
  `webapp/leads.db`. Gitignored; safe to delete to reset everything.
- **On Vercel:** Vercel's Python functions only have a writable `/tmp`, and
  it isn't kept between requests or cold starts — a SQLite file there loses
  data essentially at random, including on a plain page reload. To fix
  this, add a Postgres database from the Vercel dashboard:
  1. Project → **Storage** tab → **Create Database** → Postgres (this is
     Neon's managed Postgres, offered as a native Vercel integration).
  2. Connect it to this project. Vercel automatically sets a `POSTGRES_URL`
     env var (and a few related ones) on the project — no extra config
     needed here.
  3. Redeploy. `main.py` detects `POSTGRES_URL` (or `DATABASE_URL` as a
     fallback name) at startup and switches to Postgres automatically; the
     schema is created on first connect.

  The Places cache (`places_cache.json`) still uses ephemeral `/tmp` on
  Vercel — that's an acceptable tradeoff since it's just a performance
  cache, not user data.

## Running tests

```bash
cd webapp
python -m unittest test_api.py -v
```

Tests mock the Google Places client and use local SQLite — they never call
the live API or need a real database.

`test_db_postgres.py` exercises the Postgres-backed storage directly
against a real Postgres. It skips itself automatically if none is
reachable, so it won't fail on a machine without Postgres installed. To run
it: install Postgres locally, `createdb leadfinder_test`, then
`python -m unittest test_db_postgres.py -v` (point `TEST_DATABASE_URL` at a
different database if you'd rather not use the default name).
