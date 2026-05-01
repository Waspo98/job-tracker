import os
import tempfile
import unittest

from fastapi.testclient import TestClient


os.environ.setdefault("DISABLE_SCHEDULER", "1")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app import database as db
from app import main


def fake_check(jobs=None, error=None):
    def _check(_watch):
        return jobs or [], error

    return _check


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        db.DB_PATH = os.path.join(self.temp_dir.name, "jobs.db")
        db.init_db()
        self.client = TestClient(main.app)
        self.original_check = main.check_watch
        self.addCleanup(lambda: setattr(main, "check_watch", self.original_check))

    def _csrf(self):
        response = self.client.get("/api/session")
        self.assertEqual(response.status_code, 200)
        return response.json()["csrf_token"]

    def _register(self, email="person@example.com", password="password123"):
        token = self._csrf()
        response = self.client.post(
            "/api/auth/register",
            json={"email": email, "password": password},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["csrf_token"]

    def test_register_authenticates_session(self):
        csrf = self._register()

        response = self.client.get("/api/session")
        payload = response.json()

        self.assertTrue(payload["authenticated"])
        self.assertEqual(payload["user"]["email"], "person@example.com")
        self.assertTrue(csrf)

    def test_create_watch_saves_jobs_and_returns_dashboard_state(self):
        token = self._register()
        main.check_watch = fake_check([
            {
                "job_id": "job-1",
                "title": "Mechanical Engineer",
                "location": "Chicago, IL",
                "url": "https://example.com/job-1",
            }
        ])

        response = self.client.post(
            "/api/watches",
            json={
                "company_name": "Example Co",
                "careers_url": "https://example.com/careers",
                "keywords": "engineer",
            },
            headers={"X-CSRF-Token": token},
        )
        payload = response.json()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(payload["watch"]["company_name"], "Example Co")
        self.assertEqual(payload["watch"]["job_count"], 1)
        self.assertEqual(payload["stats"]["alerts"], 1)
        self.assertEqual(payload["stats"]["jobs"], 1)

    def test_notification_update_returns_watch_without_fragment_html(self):
        token = self._register()
        watch_id = db.add_watch(
            1,
            "Notify Co",
            "https://notify.example/careers",
            "custom",
            None,
            "",
        )

        response = self.client.patch(
            f"/api/watches/{watch_id}/notifications",
            json={"email_enabled": False, "push_enabled": True},
            headers={"X-CSRF-Token": token},
        )
        payload = response.json()
        watch = db.get_watch_for_user(watch_id, 1)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(payload["watch"]["email_enabled"], False)
        self.assertEqual(payload["watch"]["push_enabled"], True)
        self.assertNotIn("html", payload)
        self.assertEqual(watch["email_enabled"], 0)
        self.assertEqual(watch["push_enabled"], 1)

    def test_reorder_persists_alert_order(self):
        token = self._register()
        first = db.add_watch(1, "First Co", "https://first.example/careers", "custom", None, "")
        second = db.add_watch(1, "Second Co", "https://second.example/careers", "custom", None, "")

        response = self.client.post(
            "/api/watches/reorder",
            json={"watch_ids": [first, second]},
            headers={"X-CSRF-Token": token},
        )
        payload = response.json()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([watch["id"] for watch in payload["watches"]], [first, second])
        self.assertEqual([watch["id"] for watch in db.get_watches_for_user(1)], [first, second])

    def test_check_watch_updates_existing_alert(self):
        token = self._register()
        watch_id = db.add_watch(1, "Check Co", "https://check.example/careers", "custom", None, "")
        main.check_watch = fake_check([
            {
                "job_id": "job-1",
                "title": "Product Designer",
                "location": "Remote",
                "url": "https://check.example/job-1",
            }
        ])

        response = self.client.post(
            f"/api/watches/{watch_id}/check",
            headers={"X-CSRF-Token": token},
        )
        payload = response.json()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(payload["watch"]["job_count"], 1)
        self.assertEqual(payload["stats"]["jobs"], 1)


if __name__ == "__main__":
    unittest.main()
