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


class ParsingAndSafetyTests(unittest.TestCase):
    def test_keyword_matching_is_case_insensitive_and_any_keyword(self):
        self.assertTrue(ats_clients._keywords_match('Senior Mechanical Engineer', 'product, mechanical'))
        self.assertFalse(ats_clients._keywords_match('Marketing Manager', 'software, hardware'))
        self.assertTrue(ats_clients._keywords_match('Marketing Manager', ''))

    def test_private_urls_are_rejected(self):
        _, error = url_safety.validate_public_http_url('http://127.0.0.1:5000/admin')

        self.assertIsNotNone(error)


class FlaskRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        db.DB_PATH = os.path.join(self.temp_dir.name, 'jobs.db')
        db.init_db()
        main.app.config.update(TESTING=True, SECRET_KEY='test-secret-key')
        self.client = main.app.test_client()

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


if __name__ == '__main__':
    unittest.main()
