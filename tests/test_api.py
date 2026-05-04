import os
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient


os.environ.setdefault("DISABLE_SCHEDULER", "1")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app import database as db
from app import ats_clients
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
        self.original_get_authentik_client = main._get_authentik_client
        self.addCleanup(lambda: setattr(main, "_get_authentik_client", self.original_get_authentik_client))
        self.original_auth_config = {
            "AUTHENTIK_ENABLED": main.AUTHENTIK_ENABLED,
            "AUTHENTIK_AUTO_REGISTER": main.AUTHENTIK_AUTO_REGISTER,
            "AUTHENTIK_REQUIRE_VERIFIED_EMAIL": main.AUTHENTIK_REQUIRE_VERIFIED_EMAIL,
            "AUTHENTIK_DISABLE_PASSWORD_LOGIN": main.AUTHENTIK_DISABLE_PASSWORD_LOGIN,
            "AUTHENTIK_DISPLAY_NAME": main.AUTHENTIK_DISPLAY_NAME,
            "AUTHENTIK_LOGIN_BUTTON_TEXT": main.AUTHENTIK_LOGIN_BUTTON_TEXT,
            "_authentik_client": main._authentik_client,
            "VAPID_PUBLIC_KEY": main.VAPID_PUBLIC_KEY,
            "VAPID_PRIVATE_KEY": main.VAPID_PRIVATE_KEY,
            "VAPID_SUBJECT": main.VAPID_SUBJECT,
            "webpush": main.webpush,
            "REGISTRATION_MODE": main.REGISTRATION_MODE,
        }
        self.addCleanup(self._restore_auth_config)

    def _restore_auth_config(self):
        for name, value in self.original_auth_config.items():
            setattr(main, name, value)

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

    def test_session_exposes_authentik_login_config(self):
        main.AUTHENTIK_ENABLED = True
        main.AUTHENTIK_LOGIN_BUTTON_TEXT = "Log in with your SSO account"
        main.AUTHENTIK_DISABLE_PASSWORD_LOGIN = False

        response = self.client.get("/api/session")
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(payload["authenticated"])
        self.assertTrue(payload["authentik_enabled"])
        self.assertEqual(payload["authentik_login_url"], "/auth/authentik/login")
        self.assertEqual(payload["authentik_login_button_text"], "Log in with your SSO account")
        self.assertTrue(payload["password_login_enabled"])

    def test_registration_is_first_user_only_by_default(self):
        main.REGISTRATION_MODE = "first-user-only"
        db.create_user("existing@example.com", "password123")

        response = self.client.get("/api/session")
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(payload["authenticated"])
        self.assertFalse(payload["registration_enabled"])

    def test_second_local_registration_is_blocked_by_default(self):
        main.REGISTRATION_MODE = "first-user-only"
        first_token = self._register(email="first@example.com")
        self.client.post("/api/auth/logout", headers={"X-CSRF-Token": first_token})
        token = self._csrf()

        response = self.client.post(
            "/api/auth/register",
            json={"email": "second@example.com", "password": "password123"},
            headers={"X-CSRF-Token": token},
        )

        self.assertEqual(response.status_code, 403, response.text)

    def test_password_auth_still_works_when_authentik_is_enabled(self):
        main.AUTHENTIK_ENABLED = True

        self._register(email="local@example.com", password="password123")
        session_payload = self.client.get("/api/session").json()

        self.assertTrue(session_payload["authenticated"])
        self.assertEqual(session_payload["user"]["email"], "local@example.com")

    def test_authentik_callback_creates_authenticated_session(self):
        class FakeAuthentikClient:
            server_metadata = {}

            async def authorize_access_token(self, _request):
                return {
                    "id_token": "id-token",
                    "userinfo": {
                        "email": "sso@example.com",
                        "email_verified": True,
                    },
                }

        main.AUTHENTIK_ENABLED = True
        main.AUTHENTIK_AUTO_REGISTER = True
        main.AUTHENTIK_REQUIRE_VERIFIED_EMAIL = True
        main._get_authentik_client = lambda: FakeAuthentikClient()

        response = self.client.get("/auth/authentik/callback", follow_redirects=False)
        session_response = self.client.get("/api/session")
        payload = session_response.json()

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/")
        self.assertEqual(session_response.status_code, 200)
        self.assertTrue(payload["authenticated"])
        self.assertEqual(payload["user"]["email"], "sso@example.com")

    def test_authentik_callback_explains_unverified_email(self):
        class FakeAuthentikClient:
            server_metadata = {}

            async def authorize_access_token(self, _request):
                return {
                    "id_token": "id-token",
                    "userinfo": {
                        "email": "sso@example.com",
                        "email_verified": False,
                    },
                }

        main.AUTHENTIK_ENABLED = True
        main.AUTHENTIK_REQUIRE_VERIFIED_EMAIL = True
        main.AUTHENTIK_DISPLAY_NAME = "Overbay.app"
        main._get_authentik_client = lambda: FakeAuthentikClient()

        response = self.client.get("/auth/authentik/callback", follow_redirects=False)
        query = parse_qs(urlparse(response.headers["location"]).query)
        session_payload = self.client.get("/api/session").json()

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            query["auth_error"][0],
            "Your SSO login worked, but Job Tracker could not sign you in because "
            "Overbay.app has not verified the email address on that account. "
            "Verify the email address in Overbay.app, then try again.",
        )
        self.assertFalse(session_payload["authenticated"])

    def test_authentik_logout_returns_provider_logout_url_and_clears_session(self):
        class FakeAuthentikClient:
            server_metadata = {
                "end_session_endpoint": "https://auth.example.com/application/o/job-tracker/end-session/"
            }

            async def authorize_access_token(self, _request):
                return {
                    "id_token": "id-token",
                    "userinfo": {
                        "email": "sso@example.com",
                        "email_verified": True,
                    },
                }

        main.AUTHENTIK_ENABLED = True
        main._get_authentik_client = lambda: FakeAuthentikClient()

        callback_response = self.client.get("/auth/authentik/callback", follow_redirects=False)
        self.assertEqual(callback_response.status_code, 303)
        token = self._csrf()

        logout_response = self.client.post("/api/auth/logout", headers={"X-CSRF-Token": token})
        payload = logout_response.json()
        fresh_session = self.client.get("/api/session").json()

        self.assertEqual(logout_response.status_code, 200, logout_response.text)
        self.assertTrue(payload["ok"])
        self.assertIn("https://auth.example.com/application/o/job-tracker/end-session/", payload["logout_url"])
        self.assertIn("id_token_hint=id-token", payload["logout_url"])
        self.assertIn("post_logout_redirect_uri=http%3A%2F%2Ftestserver%2F", payload["logout_url"])
        self.assertFalse(fresh_session["authenticated"])

    def test_local_logout_clears_session_without_provider_logout_url(self):
        token = self._register()

        response = self.client.post("/api/auth/logout", headers={"X-CSRF-Token": token})
        payload = response.json()
        fresh_session = self.client.get("/api/session").json()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["logout_url"])
        self.assertFalse(fresh_session["authenticated"])

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
        self.assertEqual(payload["stats"]["weekly_new_jobs"], 1)

    def test_negative_keywords_exclude_matching_titles(self):
        self.assertTrue(ats_clients._keywords_match("Mechanical Engineer", "engineer, -intern"))
        self.assertFalse(ats_clients._keywords_match("Mechanical Engineering Intern", "engineer, -intern"))
        self.assertTrue(ats_clients._keywords_match("Product Designer", "-intern"))

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

    def test_push_config_reports_vapid_key_availability(self):
        token = self._register()
        main.VAPID_PUBLIC_KEY = "public-key"
        main.VAPID_PRIVATE_KEY = "private-key"
        main.webpush = lambda **_kwargs: None

        response = self.client.get("/api/push/config")
        payload = response.json()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["public_key"], "public-key")
        self.assertTrue(token)

    def test_push_subscription_is_saved_for_current_user(self):
        token = self._register()
        main.VAPID_PUBLIC_KEY = "public-key"
        main.VAPID_PRIVATE_KEY = "private-key"
        main.webpush = lambda **_kwargs: None

        response = self.client.post(
            "/api/push/subscriptions",
            json={
                "endpoint": "https://push.example/subscription/1",
                "keys": {
                    "p256dh": "p256dh-key",
                    "auth": "auth-key",
                },
            },
            headers={"X-CSRF-Token": token},
        )
        subscriptions = db.get_active_push_subscriptions(1)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(subscriptions), 1)
        self.assertEqual(subscriptions[0]["endpoint"], "https://push.example/subscription/1")

    def test_push_subscription_rejects_local_endpoint(self):
        token = self._register()
        main.VAPID_PUBLIC_KEY = "public-key"
        main.VAPID_PRIVATE_KEY = "private-key"
        main.webpush = lambda **_kwargs: None

        response = self.client.post(
            "/api/push/subscriptions",
            json={
                "endpoint": "https://127.0.0.1/subscription/1",
                "keys": {
                    "p256dh": "p256dh-key",
                    "auth": "auth-key",
                },
            },
            headers={"X-CSRF-Token": token},
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("public push service", response.json()["detail"])
        self.assertEqual(len(db.get_active_push_subscriptions(1)), 0)

    def test_required_secret_rejects_example_value(self):
        original_secret = main.SECRET_KEY
        original_require_secret = os.environ.get("REQUIRE_SECRET_KEY")
        main.SECRET_KEY = "change-me-to-something-random-and-long"
        os.environ["REQUIRE_SECRET_KEY"] = "1"

        try:
            with self.assertRaisesRegex(RuntimeError, "SECRET_KEY"):
                main.validate_startup_config()
        finally:
            main.SECRET_KEY = original_secret
            if original_require_secret is None:
                os.environ.pop("REQUIRE_SECRET_KEY", None)
            else:
                os.environ["REQUIRE_SECRET_KEY"] = original_require_secret

    def test_settings_defaults_are_returned_for_current_user(self):
        self._register()

        response = self.client.get("/api/settings")
        payload = response.json()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(payload["appearance"], "system")
        self.assertTrue(payload["default_email_enabled"])
        self.assertFalse(payload["default_push_enabled"])

    def test_settings_update_controls_new_watch_notification_defaults(self):
        token = self._register()
        main.check_watch = fake_check([])

        settings_response = self.client.put(
            "/api/settings",
            json={
                "appearance": "dark",
                "default_email_enabled": False,
                "default_push_enabled": True,
                "check_interval_hours": 12,
            },
            headers={"X-CSRF-Token": token},
        )
        watch_response = self.client.post(
            "/api/watches",
            json={
                "company_name": "Defaults Co",
                "careers_url": "https://example.com/defaults-careers",
                "keywords": "",
            },
            headers={"X-CSRF-Token": token},
        )
        watch = db.get_watch_for_user(watch_response.json()["watch"]["id"], 1)

        self.assertEqual(settings_response.status_code, 200, settings_response.text)
        self.assertEqual(settings_response.json()["appearance"], "dark")
        self.assertEqual(settings_response.json()["check_interval_hours"], 12)
        self.assertEqual(watch_response.status_code, 200, watch_response.text)
        self.assertFalse(watch_response.json()["watch"]["email_enabled"])
        self.assertTrue(watch_response.json()["watch"]["push_enabled"])
        self.assertEqual(watch["email_enabled"], 0)
        self.assertEqual(watch["push_enabled"], 1)

    def test_apply_notification_defaults_updates_existing_watches(self):
        token = self._register()
        first_id = db.add_watch(
            1,
            "First Co",
            "https://first.example/careers",
            "custom",
            None,
            "",
            email_enabled=True,
            push_enabled=False,
        )
        second_id = db.add_watch(
            1,
            "Second Co",
            "https://second.example/careers",
            "custom",
            None,
            "",
            email_enabled=True,
            push_enabled=False,
        )
        self.client.put(
            "/api/settings",
            json={
                "appearance": "system",
                "default_email_enabled": False,
                "default_push_enabled": True,
                "check_interval_hours": 4,
            },
            headers={"X-CSRF-Token": token},
        )

        response = self.client.post(
            "/api/settings/notifications/apply-defaults",
            headers={"X-CSRF-Token": token},
        )
        first = db.get_watch_for_user(first_id, 1)
        second = db.get_watch_for_user(second_id, 1)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["message"], "Notification defaults applied to 2 existing alerts.")
        self.assertEqual(response.json()["stats"]["alerts"], 2)
        self.assertEqual(first["email_enabled"], 0)
        self.assertEqual(first["push_enabled"], 1)
        self.assertEqual(second["email_enabled"], 0)
        self.assertEqual(second["push_enabled"], 1)

    def test_test_notification_sends_push_to_current_user_subscription(self):
        token = self._register()
        calls = []

        def fake_webpush(**kwargs):
            calls.append(kwargs)

        main.VAPID_PUBLIC_KEY = "public-key"
        main.VAPID_PRIVATE_KEY = "private-key"
        main.webpush = fake_webpush
        db.upsert_push_subscription(1, "https://push.example/subscription/1", "p256dh-key", "auth-key")

        response = self.client.post(
            "/api/settings/test-notification",
            headers={"X-CSRF-Token": token},
        )
        payload = response.json()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(payload["message"], "Test notification sent.")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["subscription_info"]["endpoint"], "https://push.example/subscription/1")
        self.assertIn("Test notification sent from settings.", calls[0]["data"])

    def test_check_sends_push_for_push_enabled_watch(self):
        token = self._register()
        calls = []

        def fake_webpush(**kwargs):
            calls.append(kwargs)

        main.VAPID_PUBLIC_KEY = "public-key"
        main.VAPID_PRIVATE_KEY = "private-key"
        main.webpush = fake_webpush
        db.upsert_push_subscription(1, "https://push.example/subscription/1", "p256dh-key", "auth-key")
        watch_id = db.add_watch(1, "Push Co", "https://push.example/careers", "custom", None, "")
        db.set_watch_notification_settings(watch_id, 1, False, True)
        main.check_watch = fake_check([
            {
                "job_id": "job-1",
                "title": "Firmware Engineer",
                "location": "Remote",
                "url": "https://push.example/job-1",
            }
        ])

        response = self.client.post(
            f"/api/watches/{watch_id}/check",
            headers={"X-CSRF-Token": token},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["subscription_info"]["endpoint"], "https://push.example/subscription/1")
        self.assertIn("Firmware Engineer", calls[0]["data"])

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

    def test_job_notes_and_status_can_be_updated(self):
        token = self._register()
        watch_id = db.add_watch(1, "Notes Co", "https://notes.example/careers", "custom", None, "")
        db.save_job_if_new(watch_id, "job-1", "Product Manager", "Remote", "https://notes.example/job-1")
        job = db.get_jobs_for_watch(watch_id)[0]

        response = self.client.patch(
            f"/api/jobs/{job['id']}",
            json={"status": "applied", "notes": "Talked to Sam."},
            headers={"X-CSRF-Token": token},
        )
        payload = response.json()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(payload["status"], "applied")
        self.assertEqual(payload["notes"], "Talked to Sam.")

    def test_export_restore_round_trips_current_user_data(self):
        token = self._register()
        watch_id = db.add_watch(1, "Export Co", "https://export.example/careers", "custom", None, "engineer,-intern")
        db.save_job_if_new(watch_id, "job-1", "Firmware Engineer", "Remote", "https://export.example/job-1")
        job = db.get_jobs_for_watch(watch_id)[0]
        db.update_job_meta(job["id"], 1, "saved", "Looks promising.")

        export_response = self.client.get("/api/data/export")
        backup = export_response.json()
        db.delete_watch(watch_id, 1)

        restore_response = self.client.post(
            "/api/data/restore",
            json=backup,
            headers={"X-CSRF-Token": token},
        )
        watches = db.get_watches_for_user(1)
        jobs = db.get_jobs_for_watch(watches[0]["id"])

        self.assertEqual(export_response.status_code, 200, export_response.text)
        self.assertEqual(restore_response.status_code, 200, restore_response.text)
        self.assertEqual(watches[0]["company_name"], "Export Co")
        self.assertEqual(watches[0]["keywords"], "engineer,-intern")
        self.assertEqual(jobs[0]["status"], "saved")
        self.assertEqual(jobs[0]["notes"], "Looks promising.")


if __name__ == "__main__":
    unittest.main()
