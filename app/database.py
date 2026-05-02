import os
import sqlite3
import logging
import secrets
from typing import Any
from werkzeug.security import generate_password_hash, check_password_hash

DB_PATH = os.environ.get(
    'DATABASE_PATH',
    os.path.join(os.path.dirname(__file__), '..', 'data', 'jobs.db'),
)
logger = logging.getLogger(__name__)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed_watch_sort_order(conn):
    rows = conn.execute(
        "SELECT id, user_id FROM watches WHERE sort_order IS NULL "
        "ORDER BY user_id, active DESC, created_at DESC, id DESC"
    ).fetchall()
    if not rows:
        return

    max_rows = conn.execute(
        "SELECT user_id, COALESCE(MAX(sort_order), 0) AS max_order "
        "FROM watches GROUP BY user_id"
    ).fetchall()
    next_order = {row['user_id']: row['max_order'] for row in max_rows}

    for row in rows:
        user_id = row['user_id']
        next_order[user_id] = next_order.get(user_id, 0) + 1000
        conn.execute(
            "UPDATE watches SET sort_order = ? WHERE id = ?",
            (next_order[user_id], row['id']),
        )


def _row_to_dict(row):
    return dict(row) if row is not None else None


def _clean_job_status(value):
    status = str(value or '').strip().lower()
    return status if status in {'interested', 'applied', 'ignored', 'saved'} else ''


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                appearance TEXT NOT NULL DEFAULT 'system',
                default_email_enabled INTEGER DEFAULT 1,
                default_push_enabled INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS watches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                company_name TEXT NOT NULL,
                careers_url TEXT,
                ats_type TEXT,
                ats_slug TEXT,
                keywords TEXT NOT NULL DEFAULT '',
                email_enabled INTEGER DEFAULT 1,
                push_enabled INTEGER DEFAULT 0,
                sort_order INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_checked TIMESTAMP,
                last_success_at TIMESTAMP,
                last_error TEXT,
                active INTEGER DEFAULT 1,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS found_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                watch_id INTEGER NOT NULL,
                job_id TEXT NOT NULL,
                title TEXT NOT NULL,
                location TEXT,
                url TEXT,
                found_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                notified_at TIMESTAMP,
                status TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                active INTEGER DEFAULT 1,
                UNIQUE(watch_id, job_id),
                FOREIGN KEY (watch_id) REFERENCES watches(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                endpoint TEXT NOT NULL UNIQUE,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                user_agent TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                active INTEGER DEFAULT 1,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
        """)

        # Migration 1: watches table CHECK constraint removal
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='watches'"
        ).fetchone()
        if row and 'CHECK' in row['sql']:
            logger.info("Migrating watches table to remove CHECK constraint")
            conn.executescript("""
                PRAGMA foreign_keys = OFF;
                CREATE TABLE watches_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    company_name TEXT NOT NULL,
                    careers_url TEXT,
                    ats_type TEXT,
                    ats_slug TEXT,
                    keywords TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_checked TIMESTAMP,
                    last_success_at TIMESTAMP,
                    last_error TEXT,
                    active INTEGER DEFAULT 1,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                INSERT INTO watches_new
                    SELECT id, user_id, company_name, careers_url, ats_type,
                           ats_slug, keywords, created_at, last_checked, NULL, NULL, active
                    FROM watches;
                DROP TABLE watches;
                ALTER TABLE watches_new RENAME TO watches;
                PRAGMA foreign_keys = ON;
            """)
            logger.info("Watches migration complete")

        watch_cols = [r['name'] for r in conn.execute("PRAGMA table_info(watches)").fetchall()]
        if 'last_success_at' not in watch_cols:
            logger.info("Migrating watches table to add last_success_at")
            conn.execute("ALTER TABLE watches ADD COLUMN last_success_at TIMESTAMP")
        if 'last_error' not in watch_cols:
            logger.info("Migrating watches table to add last_error")
            conn.execute("ALTER TABLE watches ADD COLUMN last_error TEXT")
        if 'email_enabled' not in watch_cols:
            logger.info("Migrating watches table to add email_enabled")
            conn.execute("ALTER TABLE watches ADD COLUMN email_enabled INTEGER DEFAULT 1")
        conn.execute("UPDATE watches SET email_enabled = 1 WHERE email_enabled IS NULL")
        if 'push_enabled' not in watch_cols:
            logger.info("Migrating watches table to add push_enabled")
            conn.execute("ALTER TABLE watches ADD COLUMN push_enabled INTEGER DEFAULT 0")
        conn.execute("UPDATE watches SET push_enabled = 0 WHERE push_enabled IS NULL")
        if 'sort_order' not in watch_cols:
            logger.info("Migrating watches table to add sort_order")
            conn.execute("ALTER TABLE watches ADD COLUMN sort_order INTEGER")
        _seed_watch_sort_order(conn)

        # Migration 2: add active column to found_jobs if missing
        cols = [r['name'] for r in conn.execute("PRAGMA table_info(found_jobs)").fetchall()]
        if 'active' not in cols:
            logger.info("Migrating found_jobs table to add active column")
            conn.execute("ALTER TABLE found_jobs ADD COLUMN active INTEGER DEFAULT 1")
            conn.execute("UPDATE found_jobs SET active = 1")
            logger.info("found_jobs active-column migration complete")

        # Migration 3: track whether a discovered job was successfully emailed
        if 'notified_at' not in cols:
            logger.info("Migrating found_jobs table to add notified_at")
            conn.execute("ALTER TABLE found_jobs ADD COLUMN notified_at TIMESTAMP")
            conn.execute("UPDATE found_jobs SET notified_at = found_at WHERE notified_at IS NULL")
            logger.info("found_jobs notification migration complete")
        if 'status' not in cols:
            logger.info("Migrating found_jobs table to add status")
            conn.execute("ALTER TABLE found_jobs ADD COLUMN status TEXT NOT NULL DEFAULT ''")
        if 'notes' not in cols:
            logger.info("Migrating found_jobs table to add notes")
            conn.execute("ALTER TABLE found_jobs ADD COLUMN notes TEXT NOT NULL DEFAULT ''")

        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_watches_user_active
                ON watches(user_id, active);
            CREATE INDEX IF NOT EXISTS idx_watches_user_active_sort
                ON watches(user_id, active, sort_order);
            CREATE INDEX IF NOT EXISTS idx_watches_user_url_active
                ON watches(user_id, careers_url, active);
            CREATE INDEX IF NOT EXISTS idx_found_jobs_watch_active
                ON found_jobs(watch_id, active);
            CREATE INDEX IF NOT EXISTS idx_found_jobs_watch_found_at
                ON found_jobs(watch_id, found_at);
            CREATE INDEX IF NOT EXISTS idx_found_jobs_status
                ON found_jobs(status);
            CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user_active
                ON push_subscriptions(user_id, active);
        """)


# User helpers

def create_user(email, password):
    with get_db() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (email.lower(),)).fetchone()
        if existing:
            return None, "An account with that email already exists."
        pw_hash = generate_password_hash(password)
        conn.execute("INSERT INTO users (email, password_hash) VALUES (?, ?)", (email.lower(), pw_hash))
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email.lower(),)).fetchone()
        return user, None


def create_sso_user(email):
    return create_user(email, secrets.token_urlsafe(48))


def get_user_by_email(email):
    with get_db() as conn:
        return conn.execute("SELECT * FROM users WHERE email = ?", (email.lower(),)).fetchone()


def get_user_by_id(user_id):
    with get_db() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def verify_password(user, password):
    return check_password_hash(user['password_hash'], password)


def count_users():
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) AS total FROM users").fetchone()
        return int(row['total'] or 0)


def get_user_settings(user_id):
    with get_db() as conn:
        settings = conn.execute(
            "SELECT * FROM user_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if settings:
            return settings
        conn.execute(
            "INSERT INTO user_settings (user_id) VALUES (?)",
            (user_id,),
        )
        return conn.execute(
            "SELECT * FROM user_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()


def upsert_user_settings(user_id, appearance, default_email_enabled, default_push_enabled):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO user_settings
                (user_id, appearance, default_email_enabled, default_push_enabled, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                appearance = excluded.appearance,
                default_email_enabled = excluded.default_email_enabled,
                default_push_enabled = excluded.default_push_enabled,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                user_id,
                appearance,
                1 if default_email_enabled else 0,
                1 if default_push_enabled else 0,
            ),
        )
        return conn.execute(
            "SELECT * FROM user_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()


def get_app_setting(key, default=None):
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (key,),
        ).fetchone()
        return row['value'] if row else default


def set_app_setting(key, value):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
            """,
            (key, str(value)),
        )


def get_check_interval_hours(default=4):
    raw = get_app_setting('check_interval_hours', str(default))
    try:
        interval = int(raw)
    except (TypeError, ValueError):
        return default
    return min(168, max(1, interval))


def set_check_interval_hours(hours):
    interval = min(168, max(1, int(hours)))
    set_app_setting('check_interval_hours', interval)
    return interval


# Watch helpers

def get_watches_for_user(user_id):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM watches WHERE user_id = ? AND active = 1 "
            "ORDER BY sort_order ASC, created_at DESC, id DESC",
            (user_id,)
        ).fetchall()


def get_watch_for_user(watch_id, user_id):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM watches WHERE id = ? AND user_id = ? AND active = 1",
            (watch_id, user_id)
        ).fetchone()


def get_all_active_watches():
    with get_db() as conn:
        return conn.execute(
            "SELECT w.*, u.email as user_email FROM watches w "
            "JOIN users u ON w.user_id = u.id WHERE w.active = 1 "
            "ORDER BY w.user_id, w.sort_order ASC, w.created_at DESC, w.id DESC"
        ).fetchall()


def add_watch(
    user_id,
    company_name,
    careers_url,
    ats_type,
    ats_slug,
    keywords,
    email_enabled=True,
    push_enabled=False,
):
    """Returns the new watch's id."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT MIN(sort_order) AS first_order FROM watches WHERE user_id = ? AND active = 1",
            (user_id,),
        ).fetchone()
        first_order = row['first_order'] if row else None
        sort_order = first_order - 1000 if first_order is not None else 1000
        cur = conn.execute(
            "INSERT INTO watches "
            "(user_id, company_name, careers_url, ats_type, ats_slug, keywords, email_enabled, push_enabled, sort_order) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                company_name.strip(),
                careers_url,
                ats_type,
                ats_slug,
                keywords.strip(),
                1 if email_enabled else 0,
                1 if push_enabled else 0,
                sort_order,
            )
        )
        return cur.lastrowid


def get_active_watch_by_url(user_id, careers_url, exclude_watch_id=None):
    query = (
        "SELECT * FROM watches "
        "WHERE user_id = ? AND lower(careers_url) = lower(?) AND active = 1"
    )
    params = [user_id, careers_url]
    if exclude_watch_id is not None:
        query += " AND id != ?"
        params.append(exclude_watch_id)

    with get_db() as conn:
        return conn.execute(query, params).fetchone()


def update_watch(watch_id, user_id, company_name, careers_url, keywords):
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE watches SET company_name = ?, careers_url = ?, ats_type = 'custom', "
            "ats_slug = NULL, keywords = ?, last_error = NULL "
            "WHERE id = ? AND user_id = ? AND active = 1",
            (company_name.strip(), careers_url, keywords.strip(), watch_id, user_id)
        )
        return cur.rowcount > 0


def set_watch_email_enabled(watch_id, user_id, enabled):
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE watches SET email_enabled = ? WHERE id = ? AND user_id = ? AND active = 1",
            (1 if enabled else 0, watch_id, user_id)
        )
        return cur.rowcount > 0


def set_watch_notification_settings(watch_id, user_id, email_enabled, push_enabled):
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE watches SET email_enabled = ?, push_enabled = ? "
            "WHERE id = ? AND user_id = ? AND active = 1",
            (1 if email_enabled else 0, 1 if push_enabled else 0, watch_id, user_id),
        )
        return cur.rowcount > 0


def set_all_watch_notification_settings(user_id, email_enabled, push_enabled):
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE watches SET email_enabled = ?, push_enabled = ? "
            "WHERE user_id = ? AND active = 1",
            (1 if email_enabled else 0, 1 if push_enabled else 0, user_id),
        )
        return cur.rowcount


def upsert_push_subscription(user_id, endpoint, p256dh, auth, user_agent=None):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, user_agent, active, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(endpoint) DO UPDATE SET
                user_id = excluded.user_id,
                p256dh = excluded.p256dh,
                auth = excluded.auth,
                user_agent = excluded.user_agent,
                active = 1,
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, endpoint, p256dh, auth, user_agent),
        )
        return conn.execute(
            "SELECT * FROM push_subscriptions WHERE endpoint = ?",
            (endpoint,),
        ).fetchone()


def get_active_push_subscriptions(user_id):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM push_subscriptions WHERE user_id = ? AND active = 1 ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()


def deactivate_push_subscription(endpoint, user_id=None):
    query = "UPDATE push_subscriptions SET active = 0, updated_at = CURRENT_TIMESTAMP WHERE endpoint = ?"
    params = [endpoint]
    if user_id is not None:
        query += " AND user_id = ?"
        params.append(user_id)
    with get_db() as conn:
        cur = conn.execute(query, params)
        return cur.rowcount > 0


def reorder_watches(user_id, watch_ids):
    watch_ids = [int(watch_id) for watch_id in watch_ids]
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id FROM watches WHERE user_id = ? AND active = 1",
            (user_id,),
        ).fetchall()
        active_ids = {row['id'] for row in rows}
        if set(watch_ids) != active_ids or len(watch_ids) != len(active_ids):
            return False

        for index, watch_id in enumerate(watch_ids, start=1):
            conn.execute(
                "UPDATE watches SET sort_order = ? WHERE id = ? AND user_id = ? AND active = 1",
                (index * 1000, watch_id, user_id),
            )
        return True


def delete_watch(watch_id, user_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE watches SET active = 0 WHERE id = ? AND user_id = ?",
            (watch_id, user_id)
        )


def get_jobs_for_watch(watch_id):
    """Returns only currently active (live) jobs for a watch."""
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM found_jobs WHERE watch_id = ? AND active = 1 ORDER BY found_at DESC",
            (watch_id,)
        ).fetchall()


def get_jobs_for_watch_for_user(watch_id, user_id):
    with get_db() as conn:
        return conn.execute(
            """SELECT fj.*
               FROM found_jobs fj
               JOIN watches w ON fj.watch_id = w.id
               WHERE fj.watch_id = ? AND w.user_id = ? AND w.active = 1 AND fj.active = 1
               ORDER BY fj.found_at DESC""",
            (watch_id, user_id),
        ).fetchall()


def get_recent_jobs_for_user(user_id, limit=100):
    """Returns only active jobs across all of a user's watches."""
    with get_db() as conn:
        return conn.execute(
            """SELECT fj.*, w.company_name, w.keywords
               FROM found_jobs fj
               JOIN watches w ON fj.watch_id = w.id
               WHERE w.user_id = ? AND w.active = 1 AND fj.active = 1
               ORDER BY fj.found_at DESC LIMIT ?""",
            (user_id, limit)
        ).fetchall()


def get_weekly_new_jobs_count(user_id):
    with get_db() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS total
               FROM found_jobs fj
               JOIN watches w ON fj.watch_id = w.id
               WHERE w.user_id = ?
                 AND w.active = 1
                 AND fj.found_at >= datetime('now', '-7 days')""",
            (user_id,),
        ).fetchone()
        return int(row['total'] or 0)


def get_job_for_user(found_job_id, user_id):
    with get_db() as conn:
        return conn.execute(
            """SELECT fj.*, w.company_name, w.keywords
               FROM found_jobs fj
               JOIN watches w ON fj.watch_id = w.id
               WHERE fj.id = ? AND w.user_id = ? AND w.active = 1""",
            (found_job_id, user_id),
        ).fetchone()


def update_job_meta(found_job_id, user_id, status, notes):
    clean_status = _clean_job_status(status)
    clean_notes = str(notes or '').strip()[:4000]
    with get_db() as conn:
        cur = conn.execute(
            """UPDATE found_jobs
               SET status = ?, notes = ?
               WHERE id = ?
                 AND watch_id IN (SELECT id FROM watches WHERE user_id = ? AND active = 1)""",
            (clean_status, clean_notes, found_job_id, user_id),
        )
        if cur.rowcount <= 0:
            return None
        return conn.execute(
            """SELECT fj.*, w.company_name, w.keywords
               FROM found_jobs fj
               JOIN watches w ON fj.watch_id = w.id
               WHERE fj.id = ?""",
            (found_job_id,),
        ).fetchone()


def save_job_if_new(watch_id, job_id, title, location, url):
    """
    Returns True if this job still needs an alert email.
    Previously notified expired jobs are reactivated silently if they return.
    """
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id, active, notified_at FROM found_jobs WHERE watch_id = ? AND job_id = ?",
            (watch_id, str(job_id))
        ).fetchone()

        if existing is None:
            # Brand new: insert it and leave notified_at empty until email succeeds.
            conn.execute(
                "INSERT INTO found_jobs (watch_id, job_id, title, location, url, notified_at, status, notes, active) "
                "VALUES (?, ?, ?, ?, ?, NULL, '', '', 1)",
                (watch_id, str(job_id), title, location, url)
            )
            return True
        elif existing['active'] == 0:
            conn.execute(
                "UPDATE found_jobs SET active = 1, title = ?, location = ?, url = ? "
                "WHERE watch_id = ? AND job_id = ?",
                (title, location, url, watch_id, str(job_id))
            )
            return existing['notified_at'] is None
        else:
            conn.execute(
                "UPDATE found_jobs SET title = ?, location = ?, url = ? "
                "WHERE watch_id = ? AND job_id = ?",
                (title, location, url, watch_id, str(job_id))
            )
            return existing['notified_at'] is None


def expire_old_jobs(watch_id, current_job_ids):
    """
    Mark any previously active jobs that are no longer in current_job_ids as inactive.
    Called after each successful check so the list stays current.
    """
    with get_db() as conn:
        active_jobs = conn.execute(
            "SELECT job_id FROM found_jobs WHERE watch_id = ? AND active = 1",
            (watch_id,)
        ).fetchall()

        current_set = set(str(jid) for jid in current_job_ids)
        expired = [r['job_id'] for r in active_jobs if r['job_id'] not in current_set]

        if expired:
            placeholders = ','.join('?' * len(expired))
            conn.execute(
                f"UPDATE found_jobs SET active = 0 WHERE watch_id = ? AND job_id IN ({placeholders})",
                [watch_id] + expired
            )

        return len(expired)


def mark_jobs_notified(watch_id, job_ids):
    if not job_ids:
        return
    with get_db() as conn:
        placeholders = ','.join('?' * len(job_ids))
        conn.execute(
            f"UPDATE found_jobs SET notified_at = CURRENT_TIMESTAMP "
            f"WHERE watch_id = ? AND job_id IN ({placeholders})",
            [watch_id] + [str(job_id) for job_id in job_ids]
        )


def mark_watch_checked(watch_id, error=None):
    with get_db() as conn:
        if error:
            conn.execute(
                "UPDATE watches SET last_checked = CURRENT_TIMESTAMP, last_error = ? WHERE id = ?",
                (str(error)[:500], watch_id)
            )
        else:
            conn.execute(
                "UPDATE watches SET last_checked = CURRENT_TIMESTAMP, "
                "last_success_at = CURRENT_TIMESTAMP, last_error = NULL WHERE id = ?",
                (watch_id,)
            )


def export_user_data(user_id, check_interval_hours=4):
    with get_db() as conn:
        settings = conn.execute(
            "SELECT appearance, default_email_enabled, default_push_enabled FROM user_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        watches = conn.execute(
            "SELECT * FROM watches WHERE user_id = ? AND active = 1 ORDER BY sort_order ASC, created_at DESC, id DESC",
            (user_id,),
        ).fetchall()

        exported_watches = []
        for watch in watches:
            jobs = conn.execute(
                "SELECT * FROM found_jobs WHERE watch_id = ? ORDER BY found_at DESC, id DESC",
                (watch['id'],),
            ).fetchall()
            exported_watches.append({
                "company_name": watch['company_name'],
                "careers_url": watch['careers_url'] or "",
                "ats_type": watch['ats_type'] or "custom",
                "ats_slug": watch['ats_slug'] or "",
                "keywords": watch['keywords'] or "",
                "email_enabled": bool(watch['email_enabled']),
                "push_enabled": bool(watch['push_enabled']),
                "sort_order": watch['sort_order'],
                "created_at": watch['created_at'],
                "last_checked": watch['last_checked'],
                "last_success_at": watch['last_success_at'],
                "last_error": watch['last_error'],
                "jobs": [
                    {
                        "job_id": job['job_id'],
                        "title": job['title'],
                        "location": job['location'] or "",
                        "url": job['url'] or "",
                        "found_at": job['found_at'],
                        "notified_at": job['notified_at'],
                        "status": _clean_job_status(job['status']),
                        "notes": job['notes'] or "",
                        "active": bool(job['active']),
                    }
                    for job in jobs
                ],
            })

        return {
            "version": 1,
            "app": "job-tracker",
            "settings": {
                "appearance": settings['appearance'] if settings else "system",
                "default_email_enabled": bool(settings['default_email_enabled']) if settings else True,
                "default_push_enabled": bool(settings['default_push_enabled']) if settings else False,
                "check_interval_hours": check_interval_hours,
            },
            "watches": exported_watches,
        }


def restore_user_data(user_id, payload):
    if not isinstance(payload, dict):
        raise ValueError("Backup file must contain a JSON object.")
    if payload.get("app") not in (None, "job-tracker"):
        raise ValueError("That backup does not look like Job Tracker data.")

    settings = payload.get("settings") or {}
    watches = payload.get("watches") or []
    if not isinstance(watches, list):
        raise ValueError("Backup watches must be a list.")
    if len(watches) > 500:
        raise ValueError("Backup contains too many alerts.")

    with get_db() as conn:
        appearance = str(settings.get("appearance") or "system").strip().lower()
        if appearance not in {"system", "light", "dark"}:
            appearance = "system"
        conn.execute(
            """
            INSERT INTO user_settings (user_id, appearance, default_email_enabled, default_push_enabled, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                appearance = excluded.appearance,
                default_email_enabled = excluded.default_email_enabled,
                default_push_enabled = excluded.default_push_enabled,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                user_id,
                appearance,
                1 if settings.get("default_email_enabled", True) else 0,
                1 if settings.get("default_push_enabled", False) else 0,
            ),
        )

        conn.execute("DELETE FROM watches WHERE user_id = ?", (user_id,))

        for index, item in enumerate(watches, start=1):
            if not isinstance(item, dict):
                raise ValueError("Each alert in the backup must be an object.")
            company_name = str(item.get("company_name") or "").strip()[:120]
            careers_url = str(item.get("careers_url") or "").strip()[:2048]
            if not company_name or not careers_url:
                continue
            ats_type = str(item.get("ats_type") or "custom").strip()[:40] or "custom"
            ats_slug = str(item.get("ats_slug") or "").strip()[:200] or None
            keywords = str(item.get("keywords") or "").strip()[:300]
            sort_order = item.get("sort_order")
            try:
                sort_order = int(sort_order)
            except (TypeError, ValueError):
                sort_order = index * 1000
            cur = conn.execute(
                """
                INSERT INTO watches
                    (user_id, company_name, careers_url, ats_type, ats_slug, keywords,
                     email_enabled, push_enabled, sort_order, created_at, last_checked,
                     last_success_at, last_error, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), ?, ?, ?, 1)
                """,
                (
                    user_id,
                    company_name,
                    careers_url,
                    ats_type,
                    ats_slug,
                    keywords,
                    1 if item.get("email_enabled", True) else 0,
                    1 if item.get("push_enabled", False) else 0,
                    sort_order,
                    item.get("created_at"),
                    item.get("last_checked"),
                    item.get("last_success_at"),
                    str(item.get("last_error") or "")[:500] or None,
                ),
            )
            watch_id = cur.lastrowid
            jobs = item.get("jobs") or []
            if not isinstance(jobs, list):
                continue
            for job in jobs[:2000]:
                if not isinstance(job, dict):
                    continue
                job_id = str(job.get("job_id") or "").strip()[:300]
                title = str(job.get("title") or "").strip()[:500]
                if not job_id or not title:
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO found_jobs
                        (watch_id, job_id, title, location, url, found_at, notified_at, status, notes, active)
                    VALUES (?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), ?, ?, ?, ?)
                    """,
                    (
                        watch_id,
                        job_id,
                        title,
                        str(job.get("location") or "").strip()[:500],
                        str(job.get("url") or "").strip()[:2048],
                        job.get("found_at"),
                        job.get("notified_at"),
                        _clean_job_status(job.get("status")),
                        str(job.get("notes") or "").strip()[:4000],
                        1 if job.get("active", True) else 0,
                    ),
                )

    interval = settings.get("check_interval_hours")
    try:
        interval = set_check_interval_hours(interval)
    except (TypeError, ValueError):
        interval = get_check_interval_hours()
    return interval
