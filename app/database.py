import sqlite3
import os
import logging
import secrets
from werkzeug.security import generate_password_hash, check_password_hash

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'jobs.db')
logger = logging.getLogger(__name__)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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

            CREATE TABLE IF NOT EXISTS watches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                company_name TEXT NOT NULL,
                careers_url TEXT,
                ats_type TEXT,
                ats_slug TEXT,
                keywords TEXT NOT NULL DEFAULT '',
                email_enabled INTEGER DEFAULT 1,
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
                active INTEGER DEFAULT 1,
                UNIQUE(watch_id, job_id),
                FOREIGN KEY (watch_id) REFERENCES watches(id) ON DELETE CASCADE
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

        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_watches_user_active
                ON watches(user_id, active);
            CREATE INDEX IF NOT EXISTS idx_watches_user_url_active
                ON watches(user_id, careers_url, active);
            CREATE INDEX IF NOT EXISTS idx_found_jobs_watch_active
                ON found_jobs(watch_id, active);
            CREATE INDEX IF NOT EXISTS idx_found_jobs_watch_found_at
                ON found_jobs(watch_id, found_at);
        """)


# ── User helpers ──────────────────────────────────────────────────────────────

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


# ── Watch helpers ─────────────────────────────────────────────────────────────

def get_watches_for_user(user_id):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM watches WHERE user_id = ? AND active = 1 ORDER BY created_at DESC",
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
            "JOIN users u ON w.user_id = u.id WHERE w.active = 1"
        ).fetchall()


def add_watch(user_id, company_name, careers_url, ats_type, ats_slug, keywords):
    """Returns the new watch's id."""
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO watches (user_id, company_name, careers_url, ats_type, ats_slug, keywords) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, company_name.strip(), careers_url, ats_type, ats_slug, keywords.strip())
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
            # Brand new — insert it and leave notified_at empty until email succeeds.
            conn.execute(
                "INSERT INTO found_jobs (watch_id, job_id, title, location, url, notified_at, active) "
                "VALUES (?, ?, ?, ?, ?, NULL, 1)",
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
