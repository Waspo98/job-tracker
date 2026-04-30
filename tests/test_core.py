import os
import re
import sys
import json
import tempfile
import unittest
from pathlib import Path


os.environ.setdefault('DISABLE_SCHEDULER', '1')
os.environ.setdefault('SECRET_KEY', 'test-secret-key')

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))

import ats_clients
import database as db
import main
import url_safety


def extract_csrf(html):
    match = re.search(r'name="_csrf_token" value="([^"]+)"', html)
    if not match:
        raise AssertionError("CSRF token not found in rendered form")
    return match.group(1)


class DatabaseBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        db.DB_PATH = os.path.join(self.temp_dir.name, 'jobs.db')
        db.init_db()
        self.user, error = db.create_user('person@example.com', 'password123')
        self.assertIsNone(error)
        self.watch_id = db.add_watch(
            self.user['id'],
            'Example Co',
            'https://example.com/careers',
            'custom',
            None,
            'engineer',
        )

    def test_empty_successful_scan_expires_active_jobs(self):
        self.assertTrue(db.save_job_if_new(self.watch_id, 'job-1', 'Engineer', '', 'https://example.com/job-1'))

        expired = db.expire_old_jobs(self.watch_id, [])

        self.assertEqual(expired, 1)
        self.assertEqual(len(db.get_jobs_for_watch(self.watch_id)), 0)

    def test_email_notification_state_retries_until_marked(self):
        self.assertTrue(db.save_job_if_new(self.watch_id, 'job-1', 'Engineer', '', 'https://example.com/job-1'))
        self.assertTrue(db.save_job_if_new(self.watch_id, 'job-1', 'Engineer', '', 'https://example.com/job-1'))

        db.mark_jobs_notified(self.watch_id, ['job-1'])

        self.assertFalse(db.save_job_if_new(self.watch_id, 'job-1', 'Engineer', '', 'https://example.com/job-1'))

    def test_duplicate_active_watch_lookup(self):
        existing = db.get_active_watch_by_url(self.user['id'], 'https://example.com/careers')

        self.assertIsNotNone(existing)
        self.assertEqual(existing['id'], self.watch_id)

    def test_duplicate_lookup_can_exclude_current_watch(self):
        existing = db.get_active_watch_by_url(
            self.user['id'],
            'https://example.com/careers',
            exclude_watch_id=self.watch_id,
        )

        self.assertIsNone(existing)

    def test_email_enabled_defaults_and_can_toggle(self):
        watch = db.get_watch_for_user(self.watch_id, self.user['id'])
        self.assertEqual(watch['email_enabled'], 1)

        self.assertTrue(db.set_watch_email_enabled(self.watch_id, self.user['id'], False))

        watch = db.get_watch_for_user(self.watch_id, self.user['id'])
        self.assertEqual(watch['email_enabled'], 0)

    def test_watch_order_can_be_rearranged(self):
        second_id = db.add_watch(
            self.user['id'],
            'Second Co',
            'https://second.example/careers',
            'custom',
            None,
            '',
        )
        third_id = db.add_watch(
            self.user['id'],
            'Third Co',
            'https://third.example/careers',
            'custom',
            None,
            '',
        )

        self.assertEqual(
            [watch['id'] for watch in db.get_watches_for_user(self.user['id'])],
            [third_id, second_id, self.watch_id],
        )

        self.assertTrue(db.reorder_watches(self.user['id'], [self.watch_id, third_id, second_id]))
        self.assertEqual(
            [watch['id'] for watch in db.get_watches_for_user(self.user['id'])],
            [self.watch_id, third_id, second_id],
        )

    def test_sso_user_can_be_created_without_known_password(self):
        user, error = db.create_sso_user('sso@example.com')

        self.assertIsNone(error)
        self.assertEqual(user['email'], 'sso@example.com')
        self.assertFalse(db.verify_password(user, 'password123'))


class ParsingAndSafetyTests(unittest.TestCase):
    def test_keyword_matching_is_case_insensitive_and_any_keyword(self):
        self.assertTrue(ats_clients._keywords_match('Senior Mechanical Engineer', 'product, mechanical'))
        self.assertFalse(ats_clients._keywords_match('Marketing Manager', 'software, hardware'))
        self.assertTrue(ats_clients._keywords_match('Marketing Manager', ''))

    def test_private_urls_are_rejected(self):
        _, error = url_safety.validate_public_http_url('http://127.0.0.1:5000/admin')

        self.assertIsNotNone(error)

    def test_scan_diagnostic_describes_common_failures(self):
        diagnostic = main.scan_diagnostic('Could not fetch https://example.com/jobs: HTTP 404')

        self.assertEqual(diagnostic['title'], 'Page not found')

    def test_custom_url_detects_greenhouse_board_links(self):
        class FakeResponse:
            text = '''
                <html>
                  <body>
                    <a href="https://job-boards.greenhouse.io/voxmedia/jobs/1301262">
                      Join Vox Media's Talent Network
                    </a>
                  </body>
                </html>
            '''

            def raise_for_status(self):
                return None

        calls = []
        original_fetch = ats_clients.fetch_public_url
        original_greenhouse = ats_clients.check_greenhouse
        self.addCleanup(lambda: setattr(ats_clients, 'fetch_public_url', original_fetch))
        self.addCleanup(lambda: setattr(ats_clients, 'check_greenhouse', original_greenhouse))

        ats_clients.fetch_public_url = lambda *args, **kwargs: FakeResponse()

        def fake_greenhouse(slug, keywords):
            calls.append((slug, keywords))
            return [{
                'job_id': 'job-1',
                'title': 'Senior Media Engineer',
                'location': 'Remote',
                'url': 'https://job-boards.greenhouse.io/voxmedia/jobs/job-1',
            }], None

        ats_clients.check_greenhouse = fake_greenhouse

        jobs, error = ats_clients.check_custom_url('https://www.voxmedia.com/jobs/', 'engineer')

        self.assertIsNone(error)
        self.assertEqual(calls, [('voxmedia', 'engineer')])
        self.assertEqual(jobs[0]['title'], 'Senior Media Engineer')


class FlaskRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        db.DB_PATH = os.path.join(self.temp_dir.name, 'jobs.db')
        db.init_db()
        main.app.config.update(TESTING=True, SECRET_KEY='test-secret-key')
        self.client = main.app.test_client()

    def _login(self):
        user, error = db.create_user('person@example.com', 'password123')
        self.assertIsNone(error)
        form = self.client.get('/login').get_data(as_text=True)
        token = extract_csrf(form)
        response = self.client.post('/login', data={
            '_csrf_token': token,
            'email': 'person@example.com',
            'password': 'password123',
        })
        self.assertEqual(response.status_code, 302)
        dashboard = self.client.get('/').get_data(as_text=True)
        return user, extract_csrf(dashboard)

    def test_post_without_csrf_is_rejected(self):
        response = self.client.post('/register', data={
            'email': 'person@example.com',
            'password': 'password123',
            'confirm': 'password123',
        })

        self.assertEqual(response.status_code, 400)

    def test_pwa_assets_are_served(self):
        manifest_response = self.client.get('/manifest.webmanifest')
        manifest = json.loads(manifest_response.get_data(as_text=True))
        icon_sizes = {icon['sizes'] for icon in manifest['icons']}

        self.assertEqual(manifest_response.status_code, 200)
        self.assertIn('application/manifest+json', manifest_response.content_type)
        self.assertEqual(manifest['start_url'], '/')
        self.assertEqual(manifest['display'], 'standalone')
        self.assertIn('192x192', icon_sizes)
        self.assertIn('512x512', icon_sizes)

        worker_response = self.client.get('/sw.js')
        self.assertEqual(worker_response.status_code, 200)
        self.assertEqual(worker_response.headers['Service-Worker-Allowed'], '/')
        self.assertIn(b'CACHE_VERSION', worker_response.data)

        offline_response = self.client.get('/offline')
        self.assertEqual(offline_response.status_code, 200)
        self.assertIn(b'You are offline', offline_response.data)

        manifest_response.close()
        worker_response.close()
        offline_response.close()

        login_page = self.client.get('/login').get_data(as_text=True)
        self.assertIn('/manifest.webmanifest', login_page)
        self.assertIn("serviceWorker.register('/sw.js')", login_page)

    def test_external_next_redirect_is_ignored(self):
        db.create_user('person@example.com', 'password123')
        form = self.client.get('/login').get_data(as_text=True)
        token = extract_csrf(form)

        response = self.client.post('/login?next=https://evil.example/', data={
            '_csrf_token': token,
            'email': 'person@example.com',
            'password': 'password123',
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], '/')

    def test_authentik_button_renders_when_enabled(self):
        originals = (
            main.AUTHENTIK_ENABLED,
            main.AUTHENTIK_DISPLAY_NAME,
            main.AUTHENTIK_LOGIN_BUTTON_TEXT,
            main.AUTHENTIK_DISABLE_PASSWORD_LOGIN,
        )
        self.addCleanup(lambda: setattr(main, 'AUTHENTIK_ENABLED', originals[0]))
        self.addCleanup(lambda: setattr(main, 'AUTHENTIK_DISPLAY_NAME', originals[1]))
        self.addCleanup(lambda: setattr(main, 'AUTHENTIK_LOGIN_BUTTON_TEXT', originals[2]))
        self.addCleanup(lambda: setattr(main, 'AUTHENTIK_DISABLE_PASSWORD_LOGIN', originals[3]))
        main.AUTHENTIK_ENABLED = True
        main.AUTHENTIK_DISPLAY_NAME = 'Authentik'
        main.AUTHENTIK_LOGIN_BUTTON_TEXT = 'Log in with Overbay.app account'
        main.AUTHENTIK_DISABLE_PASSWORD_LOGIN = False

        html = self.client.get('/login?next=/jobs').get_data(as_text=True)

        self.assertIn('Log in with Overbay.app account', html)
        self.assertIn('/auth/authentik/login?next=/jobs', html)
        self.assertIn('name="password"', html)
        self.assertLess(html.index('name="password"'), html.index('Log in with Overbay.app account'))

    def test_password_login_can_be_disabled_for_sso_only(self):
        originals = (
            main.AUTHENTIK_ENABLED,
            main.AUTHENTIK_DISPLAY_NAME,
            main.AUTHENTIK_LOGIN_BUTTON_TEXT,
            main.AUTHENTIK_DISABLE_PASSWORD_LOGIN,
        )
        self.addCleanup(lambda: setattr(main, 'AUTHENTIK_ENABLED', originals[0]))
        self.addCleanup(lambda: setattr(main, 'AUTHENTIK_DISPLAY_NAME', originals[1]))
        self.addCleanup(lambda: setattr(main, 'AUTHENTIK_LOGIN_BUTTON_TEXT', originals[2]))
        self.addCleanup(lambda: setattr(main, 'AUTHENTIK_DISABLE_PASSWORD_LOGIN', originals[3]))
        main.AUTHENTIK_ENABLED = True
        main.AUTHENTIK_DISPLAY_NAME = 'Authentik'
        main.AUTHENTIK_LOGIN_BUTTON_TEXT = 'Log in with Overbay.app account'
        main.AUTHENTIK_DISABLE_PASSWORD_LOGIN = True

        html = self.client.get('/login').get_data(as_text=True)

        self.assertIn('Log in with Overbay.app account', html)
        self.assertNotIn('name="password"', html)
        self.assertNotIn('Create one', html)

    def test_authentik_startup_config_builds_callback_without_request(self):
        originals = (
            main.APP_BASE_URL,
            main.AUTHENTIK_ENABLED,
            main.AUTHENTIK_ISSUER_URL,
            main.AUTHENTIK_CLIENT_ID,
            main.AUTHENTIK_CLIENT_SECRET,
        )
        self.addCleanup(lambda: setattr(main, 'APP_BASE_URL', originals[0]))
        self.addCleanup(lambda: setattr(main, 'AUTHENTIK_ENABLED', originals[1]))
        self.addCleanup(lambda: setattr(main, 'AUTHENTIK_ISSUER_URL', originals[2]))
        self.addCleanup(lambda: setattr(main, 'AUTHENTIK_CLIENT_ID', originals[3]))
        self.addCleanup(lambda: setattr(main, 'AUTHENTIK_CLIENT_SECRET', originals[4]))
        main.APP_BASE_URL = 'https://jobs.example.com'
        main.AUTHENTIK_ENABLED = True
        main.AUTHENTIK_ISSUER_URL = 'https://auth.example.com/application/o/job-tracker/'
        main.AUTHENTIK_CLIENT_ID = 'client-id'
        main.AUTHENTIK_CLIENT_SECRET = 'client-secret'

        main.validate_startup_config()

        self.assertEqual(
            main._external_url('authentik_callback'),
            'https://jobs.example.com/auth/authentik/callback',
        )

    def test_authentik_user_requires_verified_email_by_default(self):
        original_auto = main.AUTHENTIK_AUTO_REGISTER
        original_verified = main.AUTHENTIK_REQUIRE_VERIFIED_EMAIL
        self.addCleanup(lambda: setattr(main, 'AUTHENTIK_AUTO_REGISTER', original_auto))
        self.addCleanup(lambda: setattr(main, 'AUTHENTIK_REQUIRE_VERIFIED_EMAIL', original_verified))
        main.AUTHENTIK_AUTO_REGISTER = True
        main.AUTHENTIK_REQUIRE_VERIFIED_EMAIL = True

        user, error = main._get_or_create_authentik_user({
            'email': 'sso@example.com',
            'email_verified': False,
        })

        self.assertIsNone(user)
        self.assertIn('verified', error)

    def test_authentik_user_auto_registers_verified_email(self):
        original_auto = main.AUTHENTIK_AUTO_REGISTER
        original_verified = main.AUTHENTIK_REQUIRE_VERIFIED_EMAIL
        self.addCleanup(lambda: setattr(main, 'AUTHENTIK_AUTO_REGISTER', original_auto))
        self.addCleanup(lambda: setattr(main, 'AUTHENTIK_REQUIRE_VERIFIED_EMAIL', original_verified))
        main.AUTHENTIK_AUTO_REGISTER = True
        main.AUTHENTIK_REQUIRE_VERIFIED_EMAIL = True

        user, error = main._get_or_create_authentik_user({
            'email': 'SSO@Example.com',
            'email_verified': True,
        })

        self.assertIsNone(error)
        self.assertEqual(user['email'], 'sso@example.com')

    def test_add_custom_runs_initial_check(self):
        user, token = self._login()
        calls = []
        original_validate = main.validate_public_http_url
        original_check = main.check_watch
        original_send = main.send_job_alert
        self.addCleanup(lambda: setattr(main, 'validate_public_http_url', original_validate))
        self.addCleanup(lambda: setattr(main, 'check_watch', original_check))
        self.addCleanup(lambda: setattr(main, 'send_job_alert', original_send))

        main.validate_public_http_url = lambda url: (url, None)

        def fake_check(watch):
            calls.append(dict(watch))
            return [{
                'job_id': 'job-1',
                'title': 'Product Engineer',
                'location': 'Remote',
                'url': 'https://example.com/job-1',
            }], None

        main.check_watch = fake_check
        main.send_job_alert = lambda *args, **kwargs: True

        response = self.client.post('/add-custom', data={
            '_csrf_token': token,
            'company_name': 'Example Co',
            'custom_url': 'https://example.com/careers',
            'keywords': 'engineer',
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(calls), 1)
        watches = db.get_watches_for_user(user['id'])
        self.assertEqual(len(watches), 1)
        jobs = db.get_jobs_for_watch(watches[0]['id'])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['title'], 'Product Engineer')
        self.assertIsNotNone(jobs[0]['notified_at'])

    def test_preview_runs_check_without_saving_watch(self):
        user, token = self._login()
        calls = []
        original_validate = main.validate_public_http_url
        original_check = main.check_watch
        self.addCleanup(lambda: setattr(main, 'validate_public_http_url', original_validate))
        self.addCleanup(lambda: setattr(main, 'check_watch', original_check))

        main.validate_public_http_url = lambda url: (url, None)

        def fake_check(watch):
            calls.append(dict(watch))
            return [{
                'job_id': 'job-1',
                'title': 'Product Engineer',
                'location': 'Remote',
                'url': 'https://example.com/job-1',
            }], None

        main.check_watch = fake_check

        response = self.client.post('/preview', data={
            '_csrf_token': token,
            'company_name': 'Preview Co',
            'custom_url': 'https://example.com/careers',
            'keywords': 'engineer',
        })

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Preview Co', response.data)
        self.assertIn(b'Product Engineer', response.data)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(db.get_watches_for_user(user['id'])), 0)

    def test_edit_watch_updates_fields_and_runs_check(self):
        user, token = self._login()
        watch_id = db.add_watch(
            user['id'],
            'Old Co',
            'https://old.example/careers',
            'custom',
            None,
            'engineer',
        )
        calls = []
        original_validate = main.validate_public_http_url
        original_check = main.check_watch
        original_send = main.send_job_alert
        self.addCleanup(lambda: setattr(main, 'validate_public_http_url', original_validate))
        self.addCleanup(lambda: setattr(main, 'check_watch', original_check))
        self.addCleanup(lambda: setattr(main, 'send_job_alert', original_send))

        main.validate_public_http_url = lambda url: (url, None)

        def fake_check(watch):
            calls.append(dict(watch))
            return [{
                'job_id': 'job-2',
                'title': 'Product Designer',
                'location': 'Remote',
                'url': 'https://new.example/job-2',
            }], None

        main.check_watch = fake_check
        main.send_job_alert = lambda *args, **kwargs: True

        response = self.client.post(f'/edit/{watch_id}', data={
            '_csrf_token': token,
            'company_name': 'New Co',
            'custom_url': 'https://new.example/careers',
            'keywords': 'designer',
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['company_name'], 'New Co')
        self.assertEqual(calls[0]['careers_url'], 'https://new.example/careers')
        self.assertEqual(calls[0]['keywords'], 'designer')

        updated = db.get_watch_for_user(watch_id, user['id'])
        self.assertEqual(updated['company_name'], 'New Co')
        self.assertEqual(updated['careers_url'], 'https://new.example/careers')
        self.assertEqual(updated['keywords'], 'designer')

    def test_paused_email_saves_jobs_without_sending(self):
        user, token = self._login()
        watch_id = db.add_watch(
            user['id'],
            'Quiet Co',
            'https://quiet.example/careers',
            'custom',
            None,
            'engineer',
        )
        db.set_watch_email_enabled(watch_id, user['id'], False)

        sent = []
        original_check = main.check_watch
        original_send = main.send_job_alert
        self.addCleanup(lambda: setattr(main, 'check_watch', original_check))
        self.addCleanup(lambda: setattr(main, 'send_job_alert', original_send))

        main.check_watch = lambda watch: ([{
            'job_id': 'job-quiet',
            'title': 'Platform Engineer',
            'location': 'Remote',
            'url': 'https://quiet.example/job-quiet',
        }], None)
        main.send_job_alert = lambda *args, **kwargs: sent.append(args) or True

        response = self.client.post(f'/check-watch/{watch_id}', data={'_csrf_token': token})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(sent, [])
        jobs = db.get_jobs_for_watch(watch_id)
        self.assertEqual(len(jobs), 1)
        self.assertIsNotNone(jobs[0]['notified_at'])

    def test_toggle_email_route_flips_watch_email_setting(self):
        user, token = self._login()
        watch_id = db.add_watch(
            user['id'],
            'Toggle Co',
            'https://toggle.example/careers',
            'custom',
            None,
            '',
        )

        response = self.client.post(f'/toggle-email/{watch_id}', data={'_csrf_token': token})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_watch_for_user(watch_id, user['id'])['email_enabled'], 0)

    def test_toggle_email_treats_null_as_enabled(self):
        user, token = self._login()
        watch_id = db.add_watch(
            user['id'],
            'Legacy Toggle Co',
            'https://legacy-toggle.example/careers',
            'custom',
            None,
            '',
        )
        with db.get_db() as conn:
            conn.execute("UPDATE watches SET email_enabled = NULL WHERE id = ?", (watch_id,))

        response = self.client.post(f'/toggle-email/{watch_id}', data={'_csrf_token': token})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_watch_for_user(watch_id, user['id'])['email_enabled'], 0)

    def test_toggle_email_can_return_updated_card_json(self):
        user, token = self._login()
        watch_id = db.add_watch(
            user['id'],
            'Async Co',
            'https://async.example/careers',
            'custom',
            None,
            '',
        )

        response = self.client.post(
            f'/toggle-email/{watch_id}',
            data={'_csrf_token': token},
            headers={'Accept': 'application/json', 'X-Requested-With': 'fetch'},
        )
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['replace_target'], f'#watch-fragment-{watch_id}')
        self.assertIn('notifications paused', payload['html'])
        self.assertEqual(payload['stats']['alerts'], 1)
        self.assertEqual(db.get_watch_for_user(watch_id, user['id'])['email_enabled'], 0)

    def test_check_watch_can_return_updated_card_json(self):
        user, token = self._login()
        watch_id = db.add_watch(
            user['id'],
            'Async Check Co',
            'https://async-check.example/careers',
            'custom',
            None,
            'engineer',
        )
        original_check = main.check_watch
        self.addCleanup(lambda: setattr(main, 'check_watch', original_check))
        main.check_watch = lambda watch: ([], None)

        response = self.client.post(
            f'/check-watch/{watch_id}',
            data={'_csrf_token': token},
            headers={'Accept': 'application/json', 'X-Requested-With': 'fetch'},
        )
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['replace_target'], f'#watch-fragment-{watch_id}')
        self.assertIn('Async Check Co', payload['html'])
        self.assertEqual(payload['stats']['jobs'], 0)

    def test_check_all_can_return_updated_list_json(self):
        user, token = self._login()
        db.add_watch(
            user['id'],
            'Async All Co',
            'https://async-all.example/careers',
            'custom',
            None,
            '',
        )
        original_check = main.check_watch
        self.addCleanup(lambda: setattr(main, 'check_watch', original_check))
        main.check_watch = lambda watch: ([], None)

        response = self.client.post(
            '/check-now',
            data={'_csrf_token': token},
            headers={'Accept': 'application/json', 'X-Requested-With': 'fetch'},
        )
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['replace_target'], '#watch-list')
        self.assertIn('Async All Co', payload['html'])
        self.assertEqual(payload['stats']['alerts'], 1)

    def test_reorder_watches_route_persists_card_order(self):
        user, token = self._login()
        first_id = db.add_watch(
            user['id'],
            'First Co',
            'https://first.example/careers',
            'custom',
            None,
            '',
        )
        second_id = db.add_watch(
            user['id'],
            'Second Co',
            'https://second.example/careers',
            'custom',
            None,
            '',
        )
        third_id = db.add_watch(
            user['id'],
            'Third Co',
            'https://third.example/careers',
            'custom',
            None,
            '',
        )

        response = self.client.post(
            '/reorder-watches',
            json={'watch_ids': [first_id, third_id, second_id]},
            headers={'Accept': 'application/json', 'X-Requested-With': 'fetch', 'X-CSRF-Token': token},
        )
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload['ok'])
        self.assertEqual(
            [watch['id'] for watch in db.get_watches_for_user(user['id'])],
            [first_id, third_id, second_id],
        )
        self.assertIn('First Co', payload['html'])


if __name__ == '__main__':
    unittest.main()
