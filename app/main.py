import os
import hmac
import logging
import secrets
import warnings
from flask import (
    Flask, render_template, request, redirect, url_for, flash, jsonify, session,
    abort, send_from_directory, make_response, has_request_context,
)
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from apscheduler.schedulers.background import BackgroundScheduler

try:
    from authlib.deprecate import AuthlibDeprecationWarning

    warnings.filterwarnings(
        'ignore',
        message='authlib.jose module is deprecated.*',
        category=AuthlibDeprecationWarning,
    )
except ImportError:
    AuthlibDeprecationWarning = None

try:
    from authlib.integrations.flask_client import OAuth
except ImportError:  # Authlib is optional unless Authentik/OIDC is enabled.
    OAuth = None

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
oauth = OAuth(app) if OAuth else None


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


def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


CHECK_INTERVAL = _env_int('CHECK_INTERVAL_HOURS', 4, min_value=1, max_value=168)
MAX_COMPANY_LENGTH = 120
MAX_URL_LENGTH = 2048
MAX_KEYWORDS_LENGTH = 300
MAX_EMAIL_LENGTH = 254
MAX_PASSWORD_LENGTH = 256
APP_BASE_URL = os.environ.get('APP_BASE_URL', '').strip().rstrip('/')
AUTHENTIK_ENABLED = _env_bool('AUTHENTIK_ENABLED') or _env_bool('OIDC_ENABLED')
AUTHENTIK_ISSUER_URL = (
    os.environ.get('AUTHENTIK_ISSUER_URL') or
    os.environ.get('OIDC_ISSUER_URL') or
    ''
).strip()
AUTHENTIK_CLIENT_ID = (
    os.environ.get('AUTHENTIK_CLIENT_ID') or
    os.environ.get('OIDC_CLIENT_ID') or
    ''
).strip()
AUTHENTIK_CLIENT_SECRET = (
    os.environ.get('AUTHENTIK_CLIENT_SECRET') or
    os.environ.get('OIDC_CLIENT_SECRET') or
    ''
).strip()
AUTHENTIK_SCOPES = os.environ.get('AUTHENTIK_SCOPES') or os.environ.get('OIDC_SCOPES') or 'openid email profile'
AUTHENTIK_DISPLAY_NAME = os.environ.get('AUTHENTIK_DISPLAY_NAME', 'Authentik').strip() or 'Authentik'
AUTHENTIK_LOGIN_BUTTON_TEXT = (
    os.environ.get('AUTHENTIK_LOGIN_BUTTON_TEXT', '').strip() or
    os.environ.get('OIDC_LOGIN_BUTTON_TEXT', '').strip() or
    f'Log in with {AUTHENTIK_DISPLAY_NAME}'
)
AUTHENTIK_AUTO_REGISTER = _env_bool('AUTHENTIK_AUTO_REGISTER', True)
AUTHENTIK_REQUIRE_VERIFIED_EMAIL = _env_bool('AUTHENTIK_REQUIRE_VERIFIED_EMAIL', True)
AUTHENTIK_DISABLE_PASSWORD_LOGIN = _env_bool('AUTHENTIK_DISABLE_PASSWORD_LOGIN', False)
_authentik_client = None
_scheduler = None


def _get_csrf_token():
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return token


@app.context_processor
def inject_csrf_token():
    return {
        'csrf_token': _get_csrf_token,
        'authentik_enabled': AUTHENTIK_ENABLED,
        'authentik_display_name': AUTHENTIK_DISPLAY_NAME,
        'authentik_login_button_text': AUTHENTIK_LOGIN_BUTTON_TEXT,
        'password_login_enabled': not AUTHENTIK_DISABLE_PASSWORD_LOGIN,
    }


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


def _external_url(endpoint, **values):
    if has_request_context():
        path = url_for(endpoint, **values)
    else:
        with app.test_request_context(base_url=APP_BASE_URL or 'http://localhost'):
            path = url_for(endpoint, **values)
    if APP_BASE_URL:
        return f'{APP_BASE_URL}{path}'
    if has_request_context():
        return url_for(endpoint, _external=True, **values)
    with app.test_request_context(base_url='http://localhost'):
        return url_for(endpoint, _external=True, **values)


def _authentik_metadata_url():
    issuer = AUTHENTIK_ISSUER_URL.rstrip('/')
    if issuer.endswith('/.well-known/openid-configuration'):
        return issuer
    return f'{issuer}/.well-known/openid-configuration'


def _authentik_config_missing():
    missing = []
    if not AUTHENTIK_ISSUER_URL:
        missing.append('AUTHENTIK_ISSUER_URL')
    if not AUTHENTIK_CLIENT_ID:
        missing.append('AUTHENTIK_CLIENT_ID')
    if not AUTHENTIK_CLIENT_SECRET:
        missing.append('AUTHENTIK_CLIENT_SECRET')
    return missing


def _get_authentik_client():
    global _authentik_client
    if not AUTHENTIK_ENABLED:
        return None
    if OAuth is None or oauth is None:
        raise RuntimeError('AUTHENTIK_ENABLED requires authlib to be installed.')
    missing = _authentik_config_missing()
    if missing:
        raise RuntimeError(f'Missing Authentik/OIDC config: {", ".join(missing)}')
    if _authentik_client is None:
        _authentik_client = oauth.register(
            name='authentik',
            client_id=AUTHENTIK_CLIENT_ID,
            client_secret=AUTHENTIK_CLIENT_SECRET,
            server_metadata_url=_authentik_metadata_url(),
            client_kwargs={'scope': AUTHENTIK_SCOPES},
        )
    return _authentik_client


def _truthy_claim(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('1', 'true', 'yes')


def _get_or_create_authentik_user(userinfo):
    email = (userinfo.get('email') or '').strip().lower()
    if not _is_valid_email(email):
        return None, f'{AUTHENTIK_DISPLAY_NAME} did not provide a valid email address.'

    if AUTHENTIK_REQUIRE_VERIFIED_EMAIL and not _truthy_claim(userinfo.get('email_verified')):
        return None, f'{AUTHENTIK_DISPLAY_NAME} did not mark this email address as verified.'

    user_row = db.get_user_by_email(email)
    if user_row:
        return user_row, None
    if not AUTHENTIK_AUTO_REGISTER:
        return None, 'No local account exists for this email address.'
    return db.create_sso_user(email)


def _validate_alert_input(company, url, keywords):
    company = (company or '').strip()
    url = (url or '').strip()
    keywords = (keywords or '').strip()

    if not company:
        return None, None, None, 'Company name is required.'

    length_checks = (
        (company, 'Company name', MAX_COMPANY_LENGTH),
        (url, 'Careers page URL', MAX_URL_LENGTH),
        (keywords, 'Keywords', MAX_KEYWORDS_LENGTH),
    )
    for value, label, max_length in length_checks:
        if len(value) > max_length:
            return None, None, None, f'{label} must be {max_length} characters or fewer.'

    if not url or not url.startswith(('http://', 'https://')):
        return None, None, None, 'Please provide a valid URL starting with http:// or https://'

    url, url_error = validate_public_http_url(url)
    if url_error:
        return None, None, None, url_error

    return company, url, keywords, None


def _row_value(row, key, default=None):
    if row is None:
        return default
    try:
        if key in row.keys():
            return row[key]
    except AttributeError:
        pass
    try:
        return row.get(key, default)
    except AttributeError:
        return default


def _email_enabled(watch):
    value = _row_value(watch, 'email_enabled', 1)
    if value is None:
        return True
    return bool(value)


def _push_enabled(watch):
    return bool(_row_value(watch, 'push_enabled', 0))


def _job_ids(jobs):
    return [job['job_id'] for job in jobs]


def _notify_for_new_jobs(watch, recipient_email, new_jobs):
    if not new_jobs:
        return 'none'
    if not _email_enabled(watch):
        db.mark_jobs_notified(watch['id'], _job_ids(new_jobs))
        return 'paused'
    sent = send_job_alert(recipient_email, watch['company_name'], new_jobs)
    if sent:
        db.mark_jobs_notified(watch['id'], _job_ids(new_jobs))
        return 'sent'
    return 'failed'


def scan_diagnostic(error):
    text = str(error or '').strip()
    lower = text.lower()
    if not text:
        return {'title': 'No scan issue', 'detail': ''}
    if 'http 404' in lower:
        return {
            'title': 'Page not found',
            'detail': 'The careers URL returned HTTP 404. The company may have moved its jobs page.',
        }
    if 'http 403' in lower or 'forbidden' in lower:
        return {
            'title': 'Blocked by site',
            'detail': 'The careers page refused the scraper request. This site may block automated checks.',
        }
    if 'http 429' in lower or 'too many requests' in lower:
        return {
            'title': 'Rate limited',
            'detail': 'The site asked us to slow down. The next scheduled check may work.',
        }
    if 'only public careers page urls' in lower or 'embedded usernames' in lower:
        return {
            'title': 'URL blocked by safety checks',
            'detail': 'Only public HTTP/HTTPS careers pages can be checked.',
        }
    if 'too many redirects' in lower:
        return {
            'title': 'Redirect loop',
            'detail': 'The careers URL redirected too many times before reaching a page.',
        }
    if 'greenhouse slug' in lower:
        return {
            'title': 'Greenhouse board not found',
            'detail': 'A Greenhouse board was detected, but Greenhouse did not recognize the board name.',
        }
    if 'lever slug' in lower:
        return {
            'title': 'Lever board not found',
            'detail': 'A Lever board was detected, but Lever did not recognize the board name.',
        }
    if 'unsupported ats' in lower:
        return {
            'title': 'Unsupported job board',
            'detail': 'This alert points at a job board the app does not know how to check yet.',
        }
    if 'could not fetch' in lower:
        return {
            'title': 'Could not reach page',
            'detail': 'The careers page could not be fetched. The URL, network, or site may be temporarily unavailable.',
        }
    return {
        'title': 'Scan failed',
        'detail': text,
    }


@app.template_filter('scan_diagnostic')
def scan_diagnostic_filter(error):
    return scan_diagnostic(error)


def validate_startup_config():
    if not SECRET_KEY:
        message = 'SECRET_KEY is not set; sessions will be invalidated on each container restart.'
        if os.environ.get('REQUIRE_SECRET_KEY') == '1':
            raise RuntimeError(message)
        logger.warning(message)

    if AUTHENTIK_ENABLED:
        if OAuth is None:
            raise RuntimeError('AUTHENTIK_ENABLED requires authlib to be installed.')
        missing = _authentik_config_missing()
        if missing:
            raise RuntimeError(f'Missing Authentik/OIDC config: {", ".join(missing)}')
        logger.info(
            'Authentik/OIDC enabled; callback URL should be %s',
            _external_url('authentik_callback'),
        )

    if not os.environ.get('SMTP_USER') or not os.environ.get('SMTP_PASS'):
        logger.warning('SMTP_USER/SMTP_PASS are not fully configured; email alerts will be retried until SMTP works.')


@app.template_filter('domain')
def domain_from_url(url):
    """Extract bare domain from a URL for favicon lookup."""
    try:
        return urlparse(url).netloc or ''
    except Exception:
        return ''


def _wants_json():
    return (
        request.headers.get('X-Requested-With') == 'fetch' or
        'application/json' in request.headers.get('Accept', '')
    )


def _watch_data_for_row(watch):
    jobs = db.get_jobs_for_watch(watch['id'])
    return {'watch': watch, 'job_count': len(jobs), 'jobs': list(jobs[:5])}


def _watch_data_for_user(user_id):
    return [_watch_data_for_row(watch) for watch in db.get_watches_for_user(user_id)]


def _dashboard_stats(watch_data):
    return {
        'alerts': len(watch_data),
        'jobs': sum(wd['job_count'] for wd in watch_data),
        'interval': CHECK_INTERVAL,
    }


def _json_watch_response(watch_id, message, category='success', status=200):
    watch = db.get_watch_for_user(watch_id, current_user.id)
    if not watch:
        return jsonify({'ok': False, 'message': 'Alert not found.', 'category': 'error'}), 404

    watch_data = _watch_data_for_row(watch)
    all_watch_data = _watch_data_for_user(current_user.id)
    return jsonify({
        'ok': status < 400,
        'message': message,
        'category': category,
        'html': render_template('_watch_card.html', wd=watch_data),
        'replace_target': f'#watch-fragment-{watch_id}',
        'replace_mode': 'outer',
        'stats': _dashboard_stats(all_watch_data),
    }), status


def _json_watch_list_response(message, category='success', status=200):
    watch_data = _watch_data_for_user(current_user.id)
    return jsonify({
        'ok': status < 400,
        'message': message,
        'category': category,
        'html': render_template('_watch_list.html', watch_data=watch_data),
        'replace_target': '#watch-list',
        'replace_mode': 'outer',
        'stats': _dashboard_stats(watch_data),
    }), status

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
    stats = {'checked': 0, 'new_jobs': 0, 'expired': 0, 'errors': 0, 'email_failures': 0, 'email_paused': 0}

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
            notification = _notify_for_new_jobs(watch, _watch_email(watch, fallback_email), new_jobs)
            if notification == 'failed':
                stats['email_failures'] += 1
            elif notification == 'paused':
                stats['email_paused'] += len(new_jobs)

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
    if AUTHENTIK_DISABLE_PASSWORD_LOGIN:
        flash('Local account registration is disabled. Use SSO to log in.', 'info')
        return redirect(url_for('login'))
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
        if AUTHENTIK_DISABLE_PASSWORD_LOGIN:
            flash('Password login is disabled. Use SSO to continue.', 'info')
            return render_template('login.html')
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


@app.route('/auth/authentik/login')
def authentik_login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if not AUTHENTIK_ENABLED:
        flash('SSO login is not configured.', 'error')
        return redirect(url_for('login'))

    next_page = request.args.get('next')
    session['authentik_next'] = next_page if _is_safe_redirect_target(next_page) else url_for('dashboard')
    redirect_uri = _external_url('authentik_callback')
    try:
        return _get_authentik_client().authorize_redirect(redirect_uri)
    except Exception:
        logger.exception('Could not start Authentik/OIDC login')
        flash('Could not start SSO login. Check the Authentik configuration.', 'error')
        return redirect(url_for('login'))


@app.route('/auth/authentik/callback')
def authentik_callback():
    if not AUTHENTIK_ENABLED:
        flash('SSO login is not configured.', 'error')
        return redirect(url_for('login'))

    try:
        client = _get_authentik_client()
        token = client.authorize_access_token()
        userinfo = token.get('userinfo') or client.userinfo(token=token)
    except Exception:
        logger.exception('Authentik/OIDC callback failed')
        flash('SSO login failed. Please try again.', 'error')
        return redirect(url_for('login'))

    user_row, error = _get_or_create_authentik_user(dict(userinfo))
    if error:
        flash(error, 'error')
        return redirect(url_for('login'))

    login_user(User(user_row), remember=True)
    next_page = session.pop('authentik_next', None)
    if _is_safe_redirect_target(next_page):
        return redirect(next_page)
    return redirect(url_for('dashboard'))


@app.route('/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


# ── Main routes ───────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def dashboard():
    watch_data = _watch_data_for_user(current_user.id)
    return render_template('dashboard.html', watch_data=watch_data, check_interval=CHECK_INTERVAL)


@app.route('/preview', methods=['POST'])
@app.route('/preview/<int:watch_id>', methods=['POST'])
@login_required
def preview_watch(watch_id=None):
    existing_watch = None
    if watch_id is not None:
        existing_watch = db.get_watch_for_user(watch_id, current_user.id)
        if not existing_watch:
            flash('Alert not found.', 'error')
            return redirect(url_for('dashboard') + '#alerts-section')

    company, url, keywords, error = _validate_alert_input(
        request.form.get('company_name'),
        request.form.get('custom_url'),
        request.form.get('keywords'),
    )
    if error:
        flash(error, 'error')
        anchor = f'#watch-{watch_id}' if watch_id else '#add-alert'
        return redirect(url_for('dashboard') + anchor)

    duplicate = db.get_active_watch_by_url(current_user.id, url, exclude_watch_id=watch_id)
    if duplicate:
        flash('You already have an active alert for that careers page.', 'info')
        return redirect(url_for('dashboard') + f'#watch-{duplicate["id"]}')

    preview = {
        'id': watch_id or 0,
        'company_name': company,
        'careers_url': url,
        'ats_type': 'custom',
        'ats_slug': None,
        'keywords': keywords,
        'email_enabled': _row_value(existing_watch, 'email_enabled', 1),
    }
    jobs, error = check_watch(preview)
    return render_template(
        'preview.html',
        company=company,
        url=url,
        keywords=keywords,
        jobs=jobs,
        error=error,
        diagnostic=scan_diagnostic(error),
        watch_id=watch_id,
        editing=watch_id is not None,
    )


@app.route('/add-custom', methods=['POST'])
@login_required
def add_custom():
    company, url, keywords, error = _validate_alert_input(
        request.form.get('company_name'),
        request.form.get('custom_url'),
        request.form.get('keywords'),
    )
    if error:
        flash(error, 'error')
        return redirect(url_for('dashboard') + '#add-alert')
    if db.get_active_watch_by_url(current_user.id, url):
        flash('You already have an active alert for that careers page.', 'info')
        return redirect(url_for('dashboard') + '#alerts-section')

    watch_id = db.add_watch(current_user.id, company, url, 'custom', None, keywords)

    # Immediately run an initial scrape so results appear right away
    watch = db.get_watch_for_user(watch_id, current_user.id)

    if watch:
        new_jobs, expired, error = _run_check(watch)
        if error:
            flash(f'Added {company}, but the initial scan failed: {error}', 'info')
        elif new_jobs:
            notification = _notify_for_new_jobs(watch, current_user.email, new_jobs)
            if notification == 'sent':
                flash(
                    f'Added {company} — found {len(new_jobs)} matching listing{"s" if len(new_jobs) > 1 else ""} '
                    f'right away! Check your email.', 'success'
                )
            elif notification == 'paused':
                flash(
                    f'Added {company} — found {len(new_jobs)} matching listing{"s" if len(new_jobs) > 1 else ""}. '
                    f'Email is paused, so they were saved without sending an alert.', 'success'
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


@app.route('/edit/<int:watch_id>', methods=['POST'])
@login_required
def edit_watch(watch_id):
    existing_watch = db.get_watch_for_user(watch_id, current_user.id)
    if not existing_watch:
        flash('Alert not found.', 'error')
        return redirect(url_for('dashboard') + '#alerts-section')

    company, url, keywords, error = _validate_alert_input(
        request.form.get('company_name'),
        request.form.get('custom_url'),
        request.form.get('keywords'),
    )
    if error:
        flash(error, 'error')
        return redirect(url_for('dashboard') + f'#watch-{watch_id}')

    duplicate = db.get_active_watch_by_url(current_user.id, url, exclude_watch_id=watch_id)
    if duplicate:
        flash('You already have another active alert for that careers page.', 'info')
        return redirect(url_for('dashboard') + f'#watch-{watch_id}')

    if not db.update_watch(watch_id, current_user.id, company, url, keywords):
        flash('Alert not found.', 'error')
        return redirect(url_for('dashboard') + '#alerts-section')

    watch = db.get_watch_for_user(watch_id, current_user.id)
    new_jobs, expired, error = _run_check(watch)

    if error:
        flash(f'Updated {company}, but the check failed: {error}', 'info')
    elif new_jobs:
        notification = _notify_for_new_jobs(watch, current_user.email, new_jobs)
        if notification == 'sent':
            flash(
                f'Updated {company} and found {len(new_jobs)} new matching listing'
                f'{"s" if len(new_jobs) > 1 else ""}. Check your email.',
                'success',
            )
        elif notification == 'paused':
            flash(
                f'Updated {company} and found {len(new_jobs)} new matching listing'
                f'{"s" if len(new_jobs) > 1 else ""}. Email is paused, so they were saved without sending an alert.',
                'success',
            )
        else:
            flash(
                f'Updated {company} and found {len(new_jobs)} new matching listing'
                f'{"s" if len(new_jobs) > 1 else ""}, but the email alert could not be sent. '
                f'The app will retry on the next check.',
                'error',
            )
    else:
        active_count = len(db.get_jobs_for_watch(watch_id))
        message = (
            f'Updated {company}; check complete with {active_count} current matching listing'
            f'{"s" if active_count != 1 else ""}.'
        )
        if expired:
            message += f' {expired} stale listing{"s" if expired != 1 else ""} removed.'
        flash(message, 'success')

    return redirect(url_for('dashboard') + f'#watch-{watch_id}')


@app.route('/toggle-email/<int:watch_id>', methods=['POST'])
@login_required
def toggle_email(watch_id):
    watch = db.get_watch_for_user(watch_id, current_user.id)
    if not watch:
        if _wants_json():
            return jsonify({'ok': False, 'message': 'Alert not found.', 'category': 'error'}), 404
        flash('Alert not found.', 'error')
        return redirect(url_for('dashboard') + '#alerts-section')

    enabled = not _email_enabled(watch)
    if not db.set_watch_email_enabled(watch_id, current_user.id, enabled):
        if _wants_json():
            return jsonify({'ok': False, 'message': 'Could not update notification settings.', 'category': 'error'}), 500
        flash('Could not update notification settings.', 'error')
        return redirect(url_for('dashboard') + f'#watch-{watch_id}')
    if enabled:
        message = f'Email alerts resumed for {watch["company_name"]}.'
        category = 'success'
    else:
        message = f'Email alerts paused for {watch["company_name"]}. New matches will still be saved here.'
        category = 'info'
    if _wants_json():
        return _json_watch_response(watch_id, message, category)
    flash(message, category)
    return redirect(url_for('dashboard') + f'#watch-{watch_id}')


@app.route('/notifications/<int:watch_id>', methods=['POST'])
@login_required
def update_notifications(watch_id):
    watch = db.get_watch_for_user(watch_id, current_user.id)
    if not watch:
        if _wants_json():
            return jsonify({'ok': False, 'message': 'Alert not found.', 'category': 'error'}), 404
        flash('Alert not found.', 'error')
        return redirect(url_for('dashboard') + '#alerts-section')

    email_enabled = request.form.get('email_enabled') == '1'
    push_enabled = request.form.get('push_enabled') == '1'
    if not db.set_watch_notification_settings(watch_id, current_user.id, email_enabled, push_enabled):
        if _wants_json():
            return jsonify({'ok': False, 'message': 'Could not update notification settings.', 'category': 'error'}), 500
        flash('Could not update notification settings.', 'error')
        return redirect(url_for('dashboard') + f'#watch-{watch_id}')

    message = f'Notification settings saved for {watch["company_name"]}.'
    if _wants_json():
        return _json_watch_response(watch_id, message, 'success')
    flash(message, 'success')
    return redirect(url_for('dashboard') + f'#watch-{watch_id}')


@app.route('/reorder-watches', methods=['POST'])
@login_required
def reorder_watches():
    data = request.get_json(silent=True) or {}
    raw_watch_ids = data.get('watch_ids') or []
    try:
        watch_ids = [int(watch_id) for watch_id in raw_watch_ids]
    except (TypeError, ValueError):
        if _wants_json():
            return jsonify({'ok': False, 'message': 'Invalid alert order.', 'category': 'error'}), 400
        flash('Invalid alert order.', 'error')
        return redirect(url_for('dashboard') + '#alerts-section')

    if not db.reorder_watches(current_user.id, watch_ids):
        if _wants_json():
            return jsonify({'ok': False, 'message': 'Could not save that alert order.', 'category': 'error'}), 400
        flash('Could not save that alert order.', 'error')
        return redirect(url_for('dashboard') + '#alerts-section')

    if _wants_json():
        return _json_watch_list_response('Alert order saved.', 'success')
    flash('Alert order saved.', 'success')
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


@app.route('/manifest.webmanifest')
def webmanifest():
    return send_from_directory(
        app.static_folder,
        'manifest.webmanifest',
        mimetype='application/manifest+json',
    )


@app.route('/sw.js')
def service_worker():
    response = make_response(send_from_directory(
        app.static_folder,
        'sw.js',
        mimetype='application/javascript',
    ))
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Cache-Control'] = 'no-cache'
    return response


@app.route('/offline')
def offline():
    return send_from_directory(app.static_folder, 'offline.html')


@app.route('/check-now', methods=['POST'])
@login_required
def check_now():
    stats = run_user_checks(current_user.id, current_user.email)
    message = f'Check complete: {stats["checked"]} alert{"s" if stats["checked"] != 1 else ""} checked.'
    if stats['new_jobs']:
        message += f' {stats["new_jobs"]} new listing{"s" if stats["new_jobs"] != 1 else ""} found.'
    if stats['expired']:
        message += f' {stats["expired"]} stale listing{"s" if stats["expired"] != 1 else ""} removed.'
    if stats['email_paused']:
        message += f' {stats["email_paused"]} listing{"s" if stats["email_paused"] != 1 else ""} saved without email.'
    if stats['email_failures']:
        message += ' Some email alerts could not be sent and will be retried.'
        category = 'error'
    elif stats['errors']:
        message += ' Some alerts could not be checked.'
        category = 'info'
    else:
        category = 'success'
    if _wants_json():
        return _json_watch_list_response(message, category)
    flash(message, category)
    return redirect(url_for('dashboard'))


@app.route('/check-watch/<int:watch_id>', methods=['POST'])
@login_required
def check_watch_now(watch_id):
    watches = db.get_watches_for_user(current_user.id)
    watch = next((w for w in watches if w['id'] == watch_id), None)
    if not watch:
        if _wants_json():
            return jsonify({'ok': False, 'message': 'Alert not found.', 'category': 'error'}), 404
        flash('Alert not found.', 'error')
        return redirect(url_for('dashboard'))

    new_jobs, expired, error = _run_check(watch)

    if error:
        message = f'Error checking {watch["company_name"]}: {error}'
        category = 'error'
    elif new_jobs:
        notification = _notify_for_new_jobs(watch, current_user.email, new_jobs)
        if notification == 'sent':
            message = (
                f'Found {len(new_jobs)} new job{"s" if len(new_jobs) > 1 else ""} at {watch["company_name"]}! '
                f'Check your email.'
            )
            category = 'success'
        elif notification == 'paused':
            message = (
                f'Found {len(new_jobs)} new job{"s" if len(new_jobs) > 1 else ""} at {watch["company_name"]}. '
                f'Email is paused, so they were saved without sending an alert.'
            )
            category = 'success'
        else:
            message = (
                f'Found {len(new_jobs)} new job{"s" if len(new_jobs) > 1 else ""} at {watch["company_name"]}, '
                f'but the email alert could not be sent. The app will retry on the next check.'
            )
            category = 'error'
    else:
        active_count = len(db.get_jobs_for_watch(watch_id))
        message = f'{watch["company_name"]}: {active_count} current listing{"s" if active_count != 1 else ""}'
        if expired:
            message += f', {expired} removed since last check'
        message += ' - no new postings.'
        category = 'info'

    if _wants_json():
        return _json_watch_response(watch_id, message, category)
    flash(message, category)
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
