"""Tests for PostgresSearchStore against a real Postgres. Skipped
automatically when no Postgres is reachable (e.g. CI, or a machine without
Postgres installed) — point TEST_DATABASE_URL at a scratch database to run
these; defaults to a local `leadfinder_test` database."""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql:///leadfinder_test")


def _postgres_available() -> bool:
    try:
        import psycopg
    except ImportError:
        return False
    try:
        conn = psycopg.connect(TEST_DATABASE_URL, connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


POSTGRES_AVAILABLE = _postgres_available()


@unittest.skipUnless(POSTGRES_AVAILABLE, f"No reachable Postgres at {TEST_DATABASE_URL}; skipping")
class PostgresSearchStoreTests(unittest.TestCase):
    def setUp(self):
        from db import PostgresSearchStore
        self.store = PostgresSearchStore(TEST_DATABASE_URL)
        self._clear_tables()

    def tearDown(self):
        self._clear_tables()

    @staticmethod
    def _clear_tables():
        import psycopg
        conn = psycopg.connect(TEST_DATABASE_URL)
        conn.execute("TRUNCATE searches, contacted, templates")
        conn.commit()
        conn.close()

    def test_record_and_get_search(self):
        leads = [{"business_name": "Tiny Salon", "place_id": "place_1"}]
        search_id = self.store.record_search("admin", {"city": "SLC", "category": "hair salon"}, leads)
        entry = self.store.get_search(search_id)
        self.assertEqual(entry["city"], "SLC")
        self.assertEqual(entry["results"], leads)

    def test_count_recent_searches_only_counts_live_calls(self):
        self.store.record_search("admin", {"city": "SLC", "category": "gym"}, [], was_live_call=True)
        self.store.record_search("admin", {"city": "SLC", "category": "gym"}, [], was_live_call=False)
        self.assertEqual(self.store.count_recent_searches("admin", 0), 1)

    def test_set_and_list_contacted(self):
        self.store.set_contacted("place_1", True, "admin", {"business_name": "Tiny Salon", "phone": "555-1111"})
        listed = self.store.list_contacted()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["business_name"], "Tiny Salon")
        self.assertEqual(listed[0]["phone"], "555-1111")

    def test_uncontact_removes_from_list_but_preserves_details_on_re_contact(self):
        self.store.set_contacted("place_1", True, "admin", {"business_name": "Tiny Salon"})
        self.store.set_contacted("place_1", False, "admin")
        self.assertEqual(self.store.list_contacted(), [])

        # Re-marking without details should keep what was stored before —
        # this is the exact upsert path that needed a Postgres-specific fix
        # (bare column refs in ON CONFLICT DO UPDATE are ambiguous there,
        # unlike SQLite).
        self.store.set_contacted("place_1", True, "admin")
        listed = self.store.list_contacted()
        self.assertEqual(listed[0]["business_name"], "Tiny Salon")

    def test_contacted_map(self):
        self.store.set_contacted("place_1", True, "admin")
        cmap = self.store.get_contacted_map(["place_1", "place_missing"])
        self.assertTrue(cmap["place_1"]["contacted"])
        self.assertNotIn("place_missing", cmap)

    def test_templates_roundtrip(self):
        self.store.set_template("call_script", "Hi there", "admin")
        templates = self.store.get_templates(["call_script", "email_template"])
        self.assertEqual(templates["call_script"]["content"], "Hi there")
        self.assertEqual(templates["email_template"]["content"], "")

    def test_schema_reinit_is_idempotent(self):
        from db import PostgresSearchStore
        PostgresSearchStore(TEST_DATABASE_URL)  # should not raise, data untouched
        self.assertEqual(self.store.list_contacted(), [])


if __name__ == "__main__":
    unittest.main()
