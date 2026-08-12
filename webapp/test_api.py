import importlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

AUTH = ("testuser", "testpass")


class FakeGmaps:
    """Stands in for googlemaps.Client — no network calls."""

    def __init__(self):
        self.places_calls = 0
        self.place_calls = 0

    def places(self, query, page_token=None):
        self.places_calls += 1
        return {
            "results": [
                self._place("Tiny Salon", 3.9, 5),
                self._place("Big Chain Salon", 4.5, 500),
                self._place("Mid Salon", 4.6, 25),
            ]
        }

    def place(self, place_id, fields):
        self.place_calls += 1
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


class FakeEmailResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def fake_email_fetch(url, timeout=None, headers=None):
    """Stands in for requests.get in email lookups — no real network calls."""
    slug = url.rsplit("/", 1)[-1].replace(" ", "").replace("place_", "").lower()
    return FakeEmailResponse(200, f'<a href="mailto:hello@{slug}.example.com">Email us</a>')


class ApiTestCase(unittest.TestCase):
    """Each test gets a fresh SQLite file and cache file, and reloads the
    app module so its module-level SearchStore points at them."""

    RATE_LIMIT = "20"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["LEADS_DB_PATH"] = str(Path(self._tmp.name) / "leads.db")
        os.environ["WEBAPP_USER"] = AUTH[0]
        os.environ["WEBAPP_PASSWORD"] = AUTH[1]
        os.environ["GOOGLE_PLACES_API_KEY"] = "fake-key-for-tests"
        os.environ["RATE_LIMIT_PER_HOUR"] = self.RATE_LIMIT

        import find_leads
        find_leads.CACHE_FILE = Path(self._tmp.name) / "places_cache.json"

        global main
        if "main" in sys.modules:
            main = importlib.reload(sys.modules["main"])
        else:
            import main

        self.main = main
        self.fake_gmaps = FakeGmaps()
        self.main.get_gmaps_client = lambda: self.fake_gmaps
        self.main.get_email_fetch = lambda: fake_email_fetch
        self.client = TestClient(self.main.app)

    def tearDown(self):
        self._tmp.cleanup()

    def search(self, **overrides):
        body = {"city": "Salt Lake City, UT", "category": "hair salon"}
        body.update(overrides)
        return self.client.post("/api/search", json=body, auth=AUTH)


class HealthTests(ApiTestCase):
    def test_health_returns_ok_without_credentials(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        # Minimal payload only — status + which storage backend is active,
        # no DB size, cache contents, connection strings, or other
        # internal state, since this route has no auth gating it.
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(set(body.keys()), {"status", "storage"})

    def test_health_reports_sqlite_backend_in_tests(self):
        # Tests never set DATABASE_URL/POSTGRES_URL, so this pins down that
        # the detection logic picks SQLite by default (and stays that way).
        response = self.client.get("/api/health")
        self.assertEqual(response.json()["storage"], "sqlite")

    def test_health_ignores_credentials_if_sent(self):
        response = self.client.get("/api/health", auth=(AUTH[0], "wrong-password"))
        self.assertEqual(response.status_code, 200)

    def test_search_still_requires_auth(self):
        response = self.client.post("/api/search", json={"city": "Provo, UT", "category": "gym"})
        self.assertEqual(response.status_code, 401)


class SearchTests(ApiTestCase):
    def test_search_requires_auth(self):
        response = self.client.post("/api/search", json={"city": "Provo, UT", "category": "gym"})
        self.assertEqual(response.status_code, 401)

    def test_search_valid_returns_scored_leads(self):
        response = self.search()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("search_id", body)
        names = {lead["business_name"] for lead in body["leads"]}
        self.assertEqual(names, {"Tiny Salon", "Big Chain Salon", "Mid Salon"})

        by_name = {lead["business_name"]: lead for lead in body["leads"]}
        self.assertEqual(by_name["Tiny Salon"]["score"], "HIGH")
        self.assertEqual(by_name["Big Chain Salon"]["score"], "LOW")
        self.assertEqual(by_name["Big Chain Salon"]["website"], "")

        # Has a website -> email lookup ran against the fake fetch.
        self.assertEqual(by_name["Tiny Salon"]["email"], "hello@tinysalon.example.com")
        # No website -> no lookup attempted, email stays blank.
        self.assertEqual(by_name["Big Chain Salon"]["email"], "")

    def test_search_applies_filters(self):
        response = self.search(min_reviews=10)
        self.assertEqual(response.status_code, 200)
        names = {lead["business_name"] for lead in response.json()["leads"]}
        self.assertEqual(names, {"Big Chain Salon", "Mid Salon"})

    def test_search_blank_city_is_rejected(self):
        response = self.search(city="   ")
        self.assertEqual(response.status_code, 422)

    def test_search_min_reviews_greater_than_max_reviews_is_rejected(self):
        response = self.search(min_reviews=50, max_reviews=10)
        self.assertEqual(response.status_code, 400)
        self.assertIn("min_reviews", response.json()["detail"])

    def test_search_min_rating_greater_than_max_rating_is_rejected(self):
        response = self.search(min_rating=4.5, max_rating=3.0)
        self.assertEqual(response.status_code, 400)
        self.assertIn("min_rating", response.json()["detail"])


class RateLimitTests(ApiTestCase):
    RATE_LIMIT = "2"

    def test_rate_limit_kicks_in_after_n_searches(self):
        first = self.search()
        second = self.search(city="Provo, UT")
        third = self.search(city="Ogden, UT")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(third.status_code, 429)
        self.assertIn("Rate limit", third.json()["detail"])


class RateLimitLiveCallOnlyTests(ApiTestCase):
    """The rate limit should only count searches that actually hit the
    Google Places API — a search fully served from find_leads.py's own
    disk cache is free and shouldn't cost quota."""

    def test_fully_cached_search_does_not_count_toward_rate_limit(self):
        first = self.search()
        self.assertEqual(first.status_code, 200)
        count_after_first = self.main.store.count_recent_searches(AUTH[0], 0)
        self.assertEqual(count_after_first, 1)

        # Same city/category as `first` -> search results, place details,
        # and email lookups are all served from cache this time.
        second = self.search()
        self.assertEqual(second.status_code, 200)
        count_after_second = self.main.store.count_recent_searches(AUTH[0], 0)
        self.assertEqual(count_after_second, 1)

    def test_search_with_a_live_call_counts_toward_rate_limit(self):
        first = self.search()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.main.store.count_recent_searches(AUTH[0], 0), 1)

        # A different city/category hasn't been cached yet -> live call.
        second = self.search(city="Ogden, UT")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.main.store.count_recent_searches(AUTH[0], 0), 2)

    def test_cached_search_is_still_recorded_in_history(self):
        self.search()
        self.search()  # fully cached, shouldn't count toward rate limit...

        # ...but both should still show up in history.
        response = self.client.get("/api/history", auth=AUTH)
        self.assertEqual(len(response.json()), 2)


class HistoryTests(ApiTestCase):
    def test_history_requires_auth(self):
        response = self.client.get("/api/history")
        self.assertEqual(response.status_code, 401)

    def test_history_lists_past_searches(self):
        self.search()
        self.search(city="Provo, UT", category="gym")

        response = self.client.get("/api/history", auth=AUTH)
        self.assertEqual(response.status_code, 200)
        entries = response.json()
        self.assertEqual(len(entries), 2)
        cities = {entry["city"] for entry in entries}
        self.assertEqual(cities, {"Salt Lake City, UT", "Provo, UT"})
        self.assertTrue(all(entry["result_count"] == 3 for entry in entries))

    def test_history_detail_returns_full_results_without_requerying(self):
        search_response = self.search()
        search_id = search_response.json()["search_id"]
        calls_after_search = self.fake_gmaps.places_calls

        detail = self.client.get(f"/api/history/{search_id}", auth=AUTH)
        self.assertEqual(detail.status_code, 200)
        body = detail.json()
        self.assertEqual(body["city"], "Salt Lake City, UT")
        self.assertEqual(len(body["results"]), 3)

        # Reloading a past search from history must not hit the Places API again.
        self.assertEqual(self.fake_gmaps.places_calls, calls_after_search)

    def test_history_detail_missing_id_is_404(self):
        response = self.client.get("/api/history/9999", auth=AUTH)
        self.assertEqual(response.status_code, 404)


class ContactedTests(ApiTestCase):
    def test_contacted_requires_auth(self):
        response = self.client.post("/api/contacted", json={"place_id": "place_Tiny Salon", "contacted": True})
        self.assertEqual(response.status_code, 401)

    def test_search_results_default_to_not_contacted(self):
        response = self.search()
        leads = response.json()["leads"]
        self.assertTrue(all(lead["contacted"] is False for lead in leads))
        self.assertTrue(all(lead["contacted_at"] is None for lead in leads))

    def test_marking_contacted_returns_confirmation(self):
        response = self.client.post(
            "/api/contacted", json={"place_id": "place_Tiny Salon", "contacted": True}, auth=AUTH
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["place_id"], "place_Tiny Salon")
        self.assertTrue(body["contacted"])
        self.assertIsNotNone(body["contacted_at"])

    def test_contacted_status_reflected_in_next_search(self):
        self.client.post("/api/contacted", json={"place_id": "place_Tiny Salon", "contacted": True}, auth=AUTH)

        response = self.search()
        by_name = {lead["business_name"]: lead for lead in response.json()["leads"]}
        self.assertTrue(by_name["Tiny Salon"]["contacted"])
        self.assertFalse(by_name["Mid Salon"]["contacted"])

    def test_contacted_status_reflected_in_history_even_if_marked_after_the_search(self):
        search_response = self.search()
        search_id = search_response.json()["search_id"]

        # Not yet contacted at search time.
        by_name = {lead["business_name"]: lead for lead in search_response.json()["leads"]}
        self.assertFalse(by_name["Tiny Salon"]["contacted"])

        # Mark contacted afterward — history should reflect current state,
        # not the snapshot frozen when the search was recorded.
        self.client.post("/api/contacted", json={"place_id": "place_Tiny Salon", "contacted": True}, auth=AUTH)

        detail = self.client.get(f"/api/history/{search_id}", auth=AUTH)
        by_name2 = {lead["business_name"]: lead for lead in detail.json()["results"]}
        self.assertTrue(by_name2["Tiny Salon"]["contacted"])

    def test_unmarking_contacted_clears_contacted_at(self):
        self.client.post("/api/contacted", json={"place_id": "place_Tiny Salon", "contacted": True}, auth=AUTH)
        response = self.client.post(
            "/api/contacted", json={"place_id": "place_Tiny Salon", "contacted": False}, auth=AUTH
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["contacted"])
        self.assertIsNone(body["contacted_at"])


class ContactedListTests(ApiTestCase):
    def test_list_requires_auth(self):
        response = self.client.get("/api/contacted/list")
        self.assertEqual(response.status_code, 401)

    def test_list_starts_empty(self):
        response = self.client.get("/api/contacted/list", auth=AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_marking_with_details_appears_in_list(self):
        self.client.post(
            "/api/contacted",
            json={
                "place_id": "place_Tiny Salon",
                "contacted": True,
                "business_name": "Tiny Salon",
                "phone": "555-1111",
                "website": "https://tinysalon.example.com",
                "email": "hi@tinysalon.example.com",
                "city": "Salt Lake City, UT",
                "category": "hair salon",
                "score": "HIGH",
            },
            auth=AUTH,
        )

        response = self.client.get("/api/contacted/list", auth=AUTH)
        self.assertEqual(response.status_code, 200)
        entries = response.json()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["business_name"], "Tiny Salon")
        self.assertEqual(entry["email"], "hi@tinysalon.example.com")
        self.assertEqual(entry["score"], "HIGH")
        self.assertIsNotNone(entry["contacted_at"])

    def test_unmarking_removes_from_list(self):
        self.client.post(
            "/api/contacted",
            json={"place_id": "place_A", "contacted": True, "business_name": "A Salon"},
            auth=AUTH,
        )
        self.client.post("/api/contacted", json={"place_id": "place_A", "contacted": False}, auth=AUTH)

        response = self.client.get("/api/contacted/list", auth=AUTH)
        self.assertEqual(response.json(), [])

    def test_details_preserved_when_re_marking_without_details(self):
        self.client.post(
            "/api/contacted",
            json={"place_id": "place_A", "contacted": True, "business_name": "A Salon", "phone": "555-0000"},
            auth=AUTH,
        )
        self.client.post("/api/contacted", json={"place_id": "place_A", "contacted": False}, auth=AUTH)
        self.client.post("/api/contacted", json={"place_id": "place_A", "contacted": True}, auth=AUTH)

        response = self.client.get("/api/contacted/list", auth=AUTH)
        entries = response.json()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["business_name"], "A Salon")
        self.assertEqual(entries[0]["phone"], "555-0000")

    def test_list_orders_most_recently_contacted_first(self):
        self.client.post(
            "/api/contacted", json={"place_id": "place_A", "contacted": True, "business_name": "A"}, auth=AUTH
        )
        self.client.post(
            "/api/contacted", json={"place_id": "place_B", "contacted": True, "business_name": "B"}, auth=AUTH
        )

        response = self.client.get("/api/contacted/list", auth=AUTH)
        names = [entry["business_name"] for entry in response.json()]
        self.assertEqual(names, ["B", "A"])


class ContactedStatsTests(ApiTestCase):
    def test_stats_requires_auth(self):
        response = self.client.get("/api/contacted/stats")
        self.assertEqual(response.status_code, 401)

    def test_stats_start_at_zero(self):
        response = self.client.get("/api/contacted/stats", auth=AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"today": 0, "this_week": 0})

    def test_marking_contacted_increments_today_and_week(self):
        self.client.post("/api/contacted", json={"place_id": "place_A", "contacted": True}, auth=AUTH)
        self.client.post("/api/contacted", json={"place_id": "place_B", "contacted": True}, auth=AUTH)

        response = self.client.get("/api/contacted/stats", auth=AUTH)
        body = response.json()
        self.assertEqual(body["today"], 2)
        self.assertEqual(body["this_week"], 2)

    def test_unmarking_decrements_stats(self):
        self.client.post("/api/contacted", json={"place_id": "place_A", "contacted": True}, auth=AUTH)
        self.client.post("/api/contacted", json={"place_id": "place_B", "contacted": True}, auth=AUTH)
        self.client.post("/api/contacted", json={"place_id": "place_A", "contacted": False}, auth=AUTH)

        response = self.client.get("/api/contacted/stats", auth=AUTH)
        body = response.json()
        self.assertEqual(body["today"], 1)
        self.assertEqual(body["this_week"], 1)

    def test_count_contacted_since_excludes_marks_before_the_window(self):
        # Exercise the db layer directly with a controlled timestamp, since
        # the API always stamps contacted_at with the current wall clock.
        self.main.store.set_contacted("place_old", True, AUTH[0])

        far_future = time.time() + 3600 * 24 * 365
        self.assertEqual(self.main.store.count_contacted_since(far_future), 0)

        far_past = time.time() - 3600 * 24 * 365
        self.assertEqual(self.main.store.count_contacted_since(far_past), 1)


class TemplatesTests(ApiTestCase):
    def test_get_templates_requires_auth(self):
        response = self.client.get("/api/templates")
        self.assertEqual(response.status_code, 401)

    def test_put_template_requires_auth(self):
        response = self.client.put("/api/templates/call_script", json={"content": "Hi there"})
        self.assertEqual(response.status_code, 401)

    def test_get_templates_defaults_to_empty(self):
        response = self.client.get("/api/templates", auth=AUTH)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body.keys()), {"call_script", "email_template"})
        for key in ("call_script", "email_template"):
            self.assertEqual(body[key]["content"], "")
            self.assertIsNone(body[key]["updated_at"])
            self.assertIsNone(body[key]["updated_by"])

    def test_saving_a_template_persists_and_is_returned_by_get(self):
        put_response = self.client.put(
            "/api/templates/call_script", json={"content": "Hi, this is Sales calling about..."}, auth=AUTH
        )
        self.assertEqual(put_response.status_code, 200)
        body = put_response.json()
        self.assertEqual(body["key"], "call_script")
        self.assertEqual(body["content"], "Hi, this is Sales calling about...")
        self.assertIsNotNone(body["updated_at"])
        self.assertEqual(body["updated_by"], AUTH[0])

        get_response = self.client.get("/api/templates", auth=AUTH)
        self.assertEqual(
            get_response.json()["call_script"]["content"], "Hi, this is Sales calling about..."
        )
        # The other template is untouched.
        self.assertEqual(get_response.json()["email_template"]["content"], "")

    def test_saving_overwrites_previous_content(self):
        self.client.put("/api/templates/email_template", json={"content": "First draft"}, auth=AUTH)
        self.client.put("/api/templates/email_template", json={"content": "Final draft"}, auth=AUTH)

        response = self.client.get("/api/templates", auth=AUTH)
        self.assertEqual(response.json()["email_template"]["content"], "Final draft")

    def test_unknown_template_key_is_404(self):
        response = self.client.put("/api/templates/bogus_key", json={"content": "x"}, auth=AUTH)
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
