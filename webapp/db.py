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
"""


class SearchStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute(SCHEMA)
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
