"""SQLite storage for past searches — doubles as the search-history log and
the source of truth for the per-user hourly rate limit."""

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    username TEXT NOT NULL,
    city TEXT NOT NULL,
    category TEXT NOT NULL,
    min_reviews INTEGER,
    max_reviews INTEGER,
    min_rating REAL,
    max_rating REAL,
    has_website INTEGER,
    result_count INTEGER NOT NULL,
    results_json TEXT NOT NULL,
    was_live_call INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS contacted (
    place_id TEXT PRIMARY KEY,
    contacted INTEGER NOT NULL,
    contacted_at REAL,
    username TEXT
);

CREATE TABLE IF NOT EXISTS templates (
    key TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    updated_at REAL,
    updated_by TEXT
);
"""


class SearchStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def record_search(self, username: str, params: dict, leads: list, was_live_call: bool = True) -> int:
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO searches
                    (timestamp, username, city, category, min_reviews, max_reviews,
                     min_rating, max_rating, has_website, result_count, results_json, was_live_call)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    username,
                    params.get("city"),
                    params.get("category"),
                    params.get("min_reviews"),
                    params.get("max_reviews"),
                    params.get("min_rating"),
                    params.get("max_rating"),
                    _tri_state_to_int(params.get("has_website")),
                    len(leads),
                    json.dumps(leads),
                    1 if was_live_call else 0,
                ),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def count_recent_searches(self, username: str, since_epoch: float) -> int:
        """Counts only searches that actually hit the Google Places API —
        cache-hit-only searches don't cost quota and shouldn't count against
        the rate limit."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM searches
                WHERE username = ? AND timestamp >= ? AND was_live_call = 1
                """,
                (username, since_epoch),
            ).fetchone()
            return row["n"]
        finally:
            conn.close()

    def list_history(self, limit: int = 50) -> list:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT id, timestamp, username, city, category, min_reviews, max_reviews,
                       min_rating, max_rating, has_website, result_count
                FROM searches
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [_row_to_summary(row) for row in rows]
        finally:
            conn.close()

    def get_search(self, search_id: int) -> Optional[dict]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM searches WHERE id = ?", (search_id,)
            ).fetchone()
            if row is None:
                return None
            summary = _row_to_summary(row)
            summary["results"] = json.loads(row["results_json"])
            return summary
        finally:
            conn.close()

    def set_contacted(self, place_id: str, contacted: bool, username: str) -> dict:
        conn = self._connect()
        try:
            contacted_at = time.time() if contacted else None
            conn.execute(
                """
                INSERT INTO contacted (place_id, contacted, contacted_at, username)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(place_id) DO UPDATE SET
                    contacted = excluded.contacted,
                    contacted_at = excluded.contacted_at,
                    username = excluded.username
                """,
                (place_id, 1 if contacted else 0, contacted_at, username),
            )
            conn.commit()
            return {"place_id": place_id, "contacted": contacted, "contacted_at": contacted_at}
        finally:
            conn.close()

    def get_contacted_map(self, place_ids: list) -> dict:
        """Returns {place_id: {"contacted": bool, "contacted_at": float|None}}
        for whichever of the given place_ids have a contacted record."""
        if not place_ids:
            return {}
        conn = self._connect()
        try:
            placeholders = ",".join("?" for _ in place_ids)
            rows = conn.execute(
                f"SELECT place_id, contacted, contacted_at FROM contacted WHERE place_id IN ({placeholders})",
                place_ids,
            ).fetchall()
            return {
                row["place_id"]: {"contacted": bool(row["contacted"]), "contacted_at": row["contacted_at"]}
                for row in rows
            }
        finally:
            conn.close()

    def get_templates(self, keys: list) -> dict:
        """Returns {key: {"content": str, "updated_at": float|None,
        "updated_by": str|None}}. Keys with no saved row yet come back with
        content=''."""
        conn = self._connect()
        try:
            placeholders = ",".join("?" for _ in keys)
            rows = conn.execute(
                f"SELECT key, content, updated_at, updated_by FROM templates WHERE key IN ({placeholders})",
                keys,
            ).fetchall()
            saved = {
                row["key"]: {
                    "content": row["content"],
                    "updated_at": row["updated_at"],
                    "updated_by": row["updated_by"],
                }
                for row in rows
            }
            return {
                key: saved.get(key, {"content": "", "updated_at": None, "updated_by": None})
                for key in keys
            }
        finally:
            conn.close()

    def set_template(self, key: str, content: str, username: str) -> dict:
        conn = self._connect()
        try:
            updated_at = time.time()
            conn.execute(
                """
                INSERT INTO templates (key, content, updated_at, updated_by)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    content = excluded.content,
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by
                """,
                (key, content, updated_at, username),
            )
            conn.commit()
            return {"key": key, "content": content, "updated_at": updated_at, "updated_by": username}
        finally:
            conn.close()


def _tri_state_to_int(value: Optional[bool]) -> Optional[int]:
    if value is None:
        return None
    return 1 if value else 0


def _int_to_tri_state(value) -> Optional[bool]:
    if value is None:
        return None
    return bool(value)


def _row_to_summary(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "timestamp": row["timestamp"],
        "username": row["username"],
        "city": row["city"],
        "category": row["category"],
        "min_reviews": row["min_reviews"],
        "max_reviews": row["max_reviews"],
        "min_rating": row["min_rating"],
        "max_rating": row["max_rating"],
        "has_website": _int_to_tri_state(row["has_website"]),
        "result_count": row["result_count"],
    }
