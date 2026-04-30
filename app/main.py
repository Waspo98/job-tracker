import os
import hmac
import logging
import secrets
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, abort
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from apscheduler.schedulers.background import BackgroundScheduler

import database as db
from ats_clients import check_watch
from email_utils import send_job_alert
from url_safety import validate_public_http_url

from urllib.parse import urlparse, urljoin

LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
SECRET_KEY = os.environ.get('SECRET_KEY')
app.secret_key = SECRET_KEY or os.urandom(24)


def _env_int(name, default, min_value=None, max_value=None):
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f'{name} must be an integer.') from exc
    if min_value is not None and value < min_value:
        raise RuntimeError(f'{name} must be at least {min_value}.')
    if max_value is not None and value > max_value:
        raise RuntimeError(f'{name} must be at most {max_value}.')
    return value


CHECK_INTERVAL = _env_int('CHECK_INTERVAL_HOURS', 4, min_value=1, max_value=168)
MAX_COMPANY_LENGTH = 120
MAX_URL_LENGTH = 2048
MAX_KEYWORDS_LENGTH = 300
MAX_EMAIL_LENGTH = 254
MAX_PASSWORD_LENGTH = 256
_scheduler = None


def _get_csrf_token():
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return token


@app.context_processor
def inject_csrf_token():
    return {'csrf_token': _get_csrf_token}


@app.before_request
def csrf_protect():
    if request.method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
        return
    expected = session.get('_csrf_token')
    submitted = request.form.get('_csrf_token') or request.headers.get('X-CSRF-Token')
    if not expected or not submitted or not hmac.compare_digest(expected, submitted):
        abort(400)


def _is_safe_redirect_target(target):
    if not target:
        return False
    host_url = urlparse(request.host_url)
    redirect_url = urlparse(urljoin(request.host_url, target))
    return redirect_url.scheme in ('http', 'https') and redirect_url.netloc == host_url.netloc


def _is_valid_email(email):
    return email and len(email) <= MAX_EMAIL_LENGTH and '@' in email and '.' in email.rsplit('@', 1)[-1]


def _too_long(value, label, max_length):
    if len(value) <= max_length:
        return False
    flash(f'{label} must be {max_length} characters or fewer.', 'error')
    return True


def validate_startup_config():
    if not SECRET_KEY:
        message = 'SECRET_KEY is not set; sessions will be invalidated on each container restart.'
        if os.environ.get('REQUIRE_SECRET_KEY') == '1':
            raise RuntimeError(message)
        logger.warning(message)

    if not os.environ.get('SMTP_USER') or not os.environ.get('SMTP_PASS'):
        logger.warning('SMTP_USER/SMTP_PASS are not fully configured; email alerts will be retried until SMTP works.')


@app.template_filter('domain')
def domain_from_url(url):
    """Extract bare domain from a URL for favicon lookup."""
    try:
        return urlparse(url).netloc or ''
    except Exception:
        return ''

# ── Flask-Login ───────────────────────────────────────────────────────────────

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to continue.'
login_manager.login_message_category = 'info'


class User(UserMixin):
    def __init__(self, row):
        self.id = row['id']
        self.email = row['email']


@login_manager.user_loader
def load_user(user_id):
    row = db.get_user_by_id(int(user_id))
    return User(row) if row else None


# ── Shared check logic ────────────────────────────────────────────────────────

def _run_check(watch):
    """
    Run a check for one watch. Returns (new_jobs, expired_count, error).
    Saves new jobs, expires removed jobs, marks watch as checked.
    """
    runnable = (
        (watch['ats_type'] in ('greenhouse', 'lever') and watch['ats_slug']) or
        (watch['ats_type'] == 'custom' and watch['careers_url'])
    )
    if not runnable:
        return [], 0, "Not fully configured"

    jobs, error = check_watch(watch)
    if error:
        db.mark_watch_checked(watch['id'], error=error)
        return [], 0, error

    new_jobs = []
    for job in jobs:
        if db.save_job_if_new(watch['id'], job['job_id'], job['title'], job['location'], job['url']):
            new_jobs.append(job)

    current_ids = [j['job_id'] for j in jobs]
    expired = db.expire_old_jobs(watch['id'], current_ids)
    db.mark_watch_checked(watch['id'])

    return new_jobs, expired, None


def _watch_email(watch, fallback=None):
    if hasattr(watch, 'keys') and 'user_email' in watch.keys():
        return watch['user_email']
    return fallback


def _run_checks_for_watches(watches, source='checks', fallback_email=None):
    watches = list(watches)
    logger.info("[%s] Running job checks", source)
    stats = {'checked': 0, 'new_jobs': 0, 'expired': 0, 'errors': 0, 'email_failures': 0}

    if not watches:
        logger.info("[%s] No active watches", source)
        return stats

    for watch in watches:
        stats['checked'] += 1
        logger.info("[%s] Checking %s", source, watch['company_name'])
        new_jobs, expired, error = _run_check(watch)
        stats['expired'] += expired

        if error:
            stats['errors'] += 1
            logger.warning("[%s] Skipped %s: %s", source, watch['company_name'], error)
            continue

        stats['new_jobs'] += len(new_jobs)
        logger.info("[%s] %s new, %s expired", source, len(new_jobs), expired)
        if new_jobs:
            sent = send_job_alert(_watch_email(watch, fallback_email), watch['company_name'], new_jobs)
            if sent:
                db.mark_jobs_notified(watch['id'], [job['job_id'] for job in new_jobs])
            else:
                stats['email_failures'] += 1

    logger.info("[%s] Done", source)
    return stats


# ── Scheduler ─────────────────────────────────────────────────────────────────

def run_all_checks():
    watches = db.get_all_active_watches()
    return _run_checks_for_watches(watches, source='scheduler')


def run_user_checks(user_id, user_email):
    watches = db.get_watches_for_user(user_id)
    return _run_checks_for_watches(watches, source=f'user:{user_id}', fallback_email=user_email)


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm', '')
        if not _is_valid_email(email):
            flash('Please enter a valid email address.', 'error')
        elif not password:
            flash('Password is required.', 'error')
        elif password != confirm:
            flash('Passwords do not match.', 'error')
        elif len(password) < 8:
            flash('Password must be at least 8 characters.', 'error')
        elif len(password) > MAX_PASSWORD_LENGTH:
            flash(f'Password must be {MAX_PASSWORD_LENGTH} characters or fewer.', 'error')
        else:
            user_row, error = db.create_user(email, password)
            if error:
                flash(error, 'error')
            else:
                login_user(User(user_row))
                flash('Account created! Welcome.', 'success')
                return redirect(url_for('dashboard'))
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        if not _is_valid_email(email) or not password:
            flash('Invalid email or password.', 'error')
            return render_template('login.html')
        user_row = db.get_user_by_email(email)
        if user_row and db.verify_password(user_row, password):
            login_user(User(user_row), remember=True)
            next_page = request.args.get('next')
            if _is_safe_redirect_target(next_page):
                return redirect(next_page)
            return redirect(url_for('dashboard'))
        flash('Invalid email or password.', 'error')
    return render_template('login.html')


@app.route('/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


# ── Main routes ───────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def dashboard():
    watches = db.get_watches_for_user(current_user.id)
    watch_data = []
    for w in watches:
        jobs = db.get_jobs_for_watch(w['id'])
        watch_data.append({'watch': w, 'job_count': len(jobs), 'jobs': list(jobs[:5])})
    return render_template('dashboard.html', watch_data=watch_data, check_interval=CHECK_INTERVAL)


@app.route('/add-custom', methods=['POST'])
@login_required
def add_custom():
    company = request.form.get('company_name', '').strip()
    url = request.form.get('custom_url', '').strip()
    keywords = request.form.get('keywords', '').strip()

    if not company:
        flash('Company name is required.', 'error')
        return redirect(url_for('dashboard') + '#add-alert')
    if (
        _too_long(company, 'Company name', MAX_COMPANY_LENGTH) or
        _too_long(url, 'Careers page URL', MAX_URL_LENGTH) or
        _too_long(keywords, 'Keywords', MAX_KEYWORDS_LENGTH)
    ):
        return redirect(url_for('dashboard') + '#add-alert')
    if not url or not url.startswith('http'):
        flash('Please provide a valid URL starting with http:// or https://', 'error')
        return redirect(url_for('dashboard') + '#add-alert')
    url, url_error = validate_public_http_url(url)
    if url_error:
        flash(url_error, 'error')
        return redirect(url_for('dashboard') + '#add-alert')
    if db.get_active_watch_by_url(current_user.id, url):
        flash('You already have an active alert for that careers page.', 'info')
        return redirect(url_for('dashboard') + '#alerts-section')

    watch_id = db.add_watch(current_user.id, company, url, 'custom', None, keywords)

    # Immediately run an initial scrape so results appear right away
    watches = db.get_watches_for_user(current_user.id)
    watch = next((w for w in watches if w['id'] == watch_id), None)

    if watch:
        new_jobs, expired, error = _run_check(watch)
        if error:
            flash(f'Added {company}, but the initial scan failed: {error}', 'info')
        elif new_jobs:
            sent = send_job_alert(current_user.email, company, new_jobs)
            if sent:
                db.mark_jobs_notified(watch_id, [job['job_id'] for job in new_jobs])
                flash(
                    f'Added {company} — found {len(new_jobs)} matching listing{"s" if len(new_jobs) > 1 else ""} '
                    f'right away! Check your email.', 'success'
                )
            else:
                flash(
                    f'Added {company} — found {len(new_jobs)} matching listing{"s" if len(new_jobs) > 1 else ""}, '
                    f'but the email alert could not be sent. The app will retry on the next check.', 'error'
                )
        else:
            total = len(db.get_jobs_for_watch(watch_id))
            if total:
                flash(f'Added {company} — found {total} current listing{"s" if total > 1 else ""}. '
                      f'You\'ll be emailed when new ones appear.', 'success')
            else:
                flash(f'Added {company}. No listings matched your keywords on the first scan — '
                      f'will keep checking every {CHECK_INTERVAL}h.', 'info')
    else:
        flash(f'Added {company}.', 'success')

    return redirect(url_for('dashboard') + '#alerts-section')


@app.route('/delete/<int:watch_id>', methods=['POST'])
@login_required
def delete(watch_id):
    db.delete_watch(watch_id, current_user.id)
    flash('Alert removed.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/jobs')
@login_required
def jobs():
    all_jobs = db.get_recent_jobs_for_user(current_user.id, limit=100)
    return render_template('jobs.html', jobs=all_jobs)


@app.route('/check-now', methods=['POST'])
@login_required
def check_now():
    stats = run_user_checks(current_user.id, current_user.email)
    message = f'Check complete: {stats["checked"]} alert{"s" if stats["checked"] != 1 else ""} checked.'
    if stats['new_jobs']:
        message += f' {stats["new_jobs"]} new listing{"s" if stats["new_jobs"] != 1 else ""} found.'
    if stats['expired']:
        message += f' {stats["expired"]} stale listing{"s" if stats["expired"] != 1 else ""} removed.'
    if stats['email_failures']:
        flash(message + ' Some email alerts could not be sent and will be retried.', 'error')
    elif stats['errors']:
        flash(message + ' Some alerts could not be checked.', 'info')
    else:
        flash(message, 'success')
    return redirect(url_for('dashboard'))


@app.route('/check-watch/<int:watch_id>', methods=['POST'])
@login_required
def check_watch_now(watch_id):
    watches = db.get_watches_for_user(current_user.id)
    watch = next((w for w in watches if w['id'] == watch_id), None)
    if not watch:
        flash('Alert not found.', 'error')
        return redirect(url_for('dashboard'))

    new_jobs, expired, error = _run_check(watch)

    if error:
        flash(f'Error checking {watch["company_name"]}: {error}', 'error')
    elif new_jobs:
        sent = send_job_alert(current_user.email, watch['company_name'], new_jobs)
        if sent:
            db.mark_jobs_notified(watch['id'], [job['job_id'] for job in new_jobs])
            flash(
                f'Found {len(new_jobs)} new job{"s" if len(new_jobs) > 1 else ""} at {watch["company_name"]}! '
                f'Check your email.', 'success'
            )
        else:
            flash(
                f'Found {len(new_jobs)} new job{"s" if len(new_jobs) > 1 else ""} at {watch["company_name"]}, '
                f'but the email alert could not be sent. The app will retry on the next check.', 'error'
            )
    else:
        active_count = len(db.get_jobs_for_watch(watch_id))
        msg = f'{watch["company_name"]}: {active_count} current listing{"s" if active_count != 1 else ""}'
        if expired:
            msg += f', {expired} removed since last check'
        msg += ' — no new postings.'
        flash(msg, 'info')

    return redirect(url_for('dashboard') + '#alerts-section')


@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


# ── Startup ───────────────────────────────────────────────────────────────────

def start_scheduler():
    global _scheduler
    if os.environ.get('DISABLE_SCHEDULER') == '1':
        logger.info("Scheduler disabled by DISABLE_SCHEDULER=1")
        return
    if _scheduler and _scheduler.running:
        return

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        run_all_checks,
        'interval',
        hours=CHECK_INTERVAL,
        id='job_check',
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info("Scheduler started; checking every %s hours", CHECK_INTERVAL)


def bootstrap():
    validate_startup_config()
    db.init_db()
    start_scheduler()


if __name__ == '__main__':
    bootstrap()
    app.run(host='0.0.0.0', port=5055, debug=False)
