import os
import re
import sys
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


class ParsingAndSafetyTests(unittest.TestCase):
    def test_keyword_matching_is_case_insensitive_and_any_keyword(self):
        self.assertTrue(ats_clients._keywords_match('Senior Mechanical Engineer', 'product, mechanical'))
        self.assertFalse(ats_clients._keywords_match('Marketing Manager', 'software, hardware'))
        self.assertTrue(ats_clients._keywords_match('Marketing Manager', ''))

    def test_private_urls_are_rejected(self):
        _, error = url_safety.validate_public_http_url('http://127.0.0.1:5000/admin')

        self.assertIsNotNone(error)

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


if __name__ == '__main__':
    unittest.main()
