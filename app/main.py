import hmac
import logging
import os
import secrets
import warnings
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

try:
    from authlib.deprecate import AuthlibDeprecationWarning

    warnings.filterwarnings(
        "ignore",
        message="authlib.jose module is deprecated.*",
        category=AuthlibDeprecationWarning,
    )
except ImportError:
    AuthlibDeprecationWarning = None

try:
    from authlib.integrations.base_client.errors import OAuthError
    from authlib.integrations.starlette_client import OAuth
except ImportError:  # Authlib is optional unless Authentik/OIDC is enabled.
    OAuth = None
    OAuthError = Exception

from . import database as db
from .ats_clients import check_watch
from .email_utils import send_job_alert
from .url_safety import validate_public_http_url


LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DIST_DIR = APP_DIR / "frontend_dist"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


SECRET_KEY = os.environ.get("SECRET_KEY")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_HOURS", "4"))
APP_BASE_URL = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
AUTHENTIK_LOGIN_PATH = "/auth/authentik/login"
AUTHENTIK_CALLBACK_PATH = "/auth/authentik/callback"
AUTHENTIK_ENABLED = _env_bool("AUTHENTIK_ENABLED") or _env_bool("OIDC_ENABLED")
AUTHENTIK_ISSUER_URL = (
    os.environ.get("AUTHENTIK_ISSUER_URL") or
    os.environ.get("OIDC_ISSUER_URL") or
    ""
).strip()
AUTHENTIK_CLIENT_ID = (
    os.environ.get("AUTHENTIK_CLIENT_ID") or
    os.environ.get("OIDC_CLIENT_ID") or
    ""
).strip()
AUTHENTIK_CLIENT_SECRET = (
    os.environ.get("AUTHENTIK_CLIENT_SECRET") or
    os.environ.get("OIDC_CLIENT_SECRET") or
    ""
).strip()
AUTHENTIK_SCOPES = os.environ.get("AUTHENTIK_SCOPES") or os.environ.get("OIDC_SCOPES") or "openid email profile"
AUTHENTIK_DISPLAY_NAME = os.environ.get("AUTHENTIK_DISPLAY_NAME", "Authentik").strip() or "Authentik"
AUTHENTIK_LOGIN_BUTTON_TEXT = (
    os.environ.get("AUTHENTIK_LOGIN_BUTTON_TEXT", "").strip() or
    os.environ.get("OIDC_LOGIN_BUTTON_TEXT", "").strip() or
    f"Log in with {AUTHENTIK_DISPLAY_NAME}"
)
AUTHENTIK_AUTO_REGISTER = _env_bool("AUTHENTIK_AUTO_REGISTER", True)
AUTHENTIK_REQUIRE_VERIFIED_EMAIL = _env_bool("AUTHENTIK_REQUIRE_VERIFIED_EMAIL", True)
AUTHENTIK_DISABLE_PASSWORD_LOGIN = _env_bool("AUTHENTIK_DISABLE_PASSWORD_LOGIN", False)
MAX_COMPANY_LENGTH = 120
MAX_URL_LENGTH = 2048
MAX_KEYWORDS_LENGTH = 300
MAX_EMAIL_LENGTH = 254
MAX_PASSWORD_LENGTH = 256

oauth = OAuth() if OAuth else None
_authentik_client: Any | None = None
_scheduler: BackgroundScheduler | None = None


app = FastAPI(title="Job Tracker API")
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY or secrets.token_urlsafe(32),
    session_cookie="job_tracker_session",
    same_site="lax",
    https_only=False,
)


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
if (DIST_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=DIST_DIR / "assets"), name="assets")


class UserOut(BaseModel):
    id: int
    email: str


class SessionOut(BaseModel):
    authenticated: bool
    csrf_token: str
    user: UserOut | None = None
    check_interval: int = CHECK_INTERVAL
    authentik_enabled: bool = False
    authentik_login_url: str | None = None
    authentik_login_button_text: str = ""
    password_login_enabled: bool = True


class LogoutOut(BaseModel):
    ok: bool = True
    logout_url: str | None = None


class LoginIn(BaseModel):
    email: str = Field(max_length=MAX_EMAIL_LENGTH)
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)


class RegisterIn(LoginIn):
    pass


class WatchIn(BaseModel):
    company_name: str = Field(min_length=1, max_length=MAX_COMPANY_LENGTH)
    careers_url: str = Field(min_length=1, max_length=MAX_URL_LENGTH)
    keywords: str = Field(default="", max_length=MAX_KEYWORDS_LENGTH)


class NotificationIn(BaseModel):
    email_enabled: bool
    push_enabled: bool = False


class ReorderIn(BaseModel):
    watch_ids: list[int]


class JobOut(BaseModel):
    id: int | None = None
    watch_id: int | None = None
    job_id: str
    title: str
    location: str = ""
    url: str = ""
    found_at: str | None = None
    notified_at: str | None = None
    company_name: str | None = None
    keywords: str | None = None


class DiagnosticOut(BaseModel):
    title: str
    detail: str


class WatchOut(BaseModel):
    id: int
    company_name: str
    careers_url: str
    keywords: str
    email_enabled: bool
    push_enabled: bool
    created_at: str | None
    last_checked: str | None
    last_success_at: str | None
    last_error: str | None
    diagnostic: DiagnosticOut | None
    job_count: int
    jobs: list[JobOut]


class StatsOut(BaseModel):
    alerts: int
    jobs: int
    interval: int


class DashboardOut(BaseModel):
    watches: list[WatchOut]
    stats: StatsOut


class ActionOut(BaseModel):
    ok: bool = True
    message: str
    category: str = "success"
    watch: WatchOut | None = None
    watches: list[WatchOut] | None = None
    stats: StatsOut | None = None


class PreviewOut(BaseModel):
    company_name: str
    careers_url: str
    keywords: str
    jobs: list[JobOut]
    error: str | None = None
    diagnostic: DiagnosticOut | None = None


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        if key in row.keys():
            return row[key]
    except AttributeError:
        pass
    if isinstance(row, dict):
        return row.get(key, default)
    return default


def _is_valid_email(email: str) -> bool:
    return bool(email and len(email) <= MAX_EMAIL_LENGTH and "@" in email and "." in email.rsplit("@", 1)[-1])


def _email_enabled(watch: Any) -> bool:
    value = _row_value(watch, "email_enabled", 1)
    if value is None:
        return True
    return bool(value)


def _push_enabled(watch: Any) -> bool:
    return bool(_row_value(watch, "push_enabled", 0))


def _get_csrf_token(request: Request) -> str:
    token = request.session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["_csrf_token"] = token
    return token


def _authentik_metadata_url() -> str:
    issuer = AUTHENTIK_ISSUER_URL.rstrip("/")
    if issuer.endswith("/.well-known/openid-configuration"):
        return issuer
    return f"{issuer}/.well-known/openid-configuration"


def _authentik_config_missing() -> list[str]:
    missing = []
    if not AUTHENTIK_ISSUER_URL:
        missing.append("AUTHENTIK_ISSUER_URL")
    if not AUTHENTIK_CLIENT_ID:
        missing.append("AUTHENTIK_CLIENT_ID")
    if not AUTHENTIK_CLIENT_SECRET:
        missing.append("AUTHENTIK_CLIENT_SECRET")
    return missing


def _get_authentik_client() -> Any:
    global _authentik_client
    if not AUTHENTIK_ENABLED:
        return None
    if OAuth is None or oauth is None:
        raise RuntimeError("AUTHENTIK_ENABLED requires authlib to be installed.")
    missing = _authentik_config_missing()
    if missing:
        raise RuntimeError(f"Missing Authentik/OIDC config: {', '.join(missing)}")
    if _authentik_client is None:
        _authentik_client = oauth.register(
            name="authentik",
            client_id=AUTHENTIK_CLIENT_ID,
            client_secret=AUTHENTIK_CLIENT_SECRET,
            server_metadata_url=_authentik_metadata_url(),
            client_kwargs={"scope": AUTHENTIK_SCOPES},
        )
    return _authentik_client


def _truthy_claim(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _get_or_create_authentik_user(userinfo: dict[str, Any]) -> tuple[Any | None, str | None]:
    email = str(userinfo.get("email") or "").strip().lower()
    if not _is_valid_email(email):
        return None, f"{AUTHENTIK_DISPLAY_NAME} did not provide a valid email address."

    if AUTHENTIK_REQUIRE_VERIFIED_EMAIL and not _truthy_claim(userinfo.get("email_verified")):
        return None, f"{AUTHENTIK_DISPLAY_NAME} did not mark this email address as verified."

    user = db.get_user_by_email(email)
    if user:
        return user, None
    if not AUTHENTIK_AUTO_REGISTER:
        return None, "No local account exists for this email address."
    return db.create_sso_user(email)


def _external_url_for_path(request: Request, path: str) -> str:
    if not path.startswith("/"):
        path = f"/{path}"
    if APP_BASE_URL:
        return f"{APP_BASE_URL}{path}"
    base_url = str(request.base_url).rstrip("/")
    return f"{base_url}{path}"


def _configured_external_url_for_path(path: str) -> str:
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{APP_BASE_URL}{path}" if APP_BASE_URL else path


def _is_safe_redirect_target(target: str | None, request: Request) -> bool:
    if not target:
        return False
    base = APP_BASE_URL or str(request.base_url)
    host_url = urlparse(base if base.endswith("/") else f"{base}/")
    redirect_url = urlparse(urljoin(f"{host_url.scheme}://{host_url.netloc}/", target))
    return redirect_url.scheme in ("http", "https") and redirect_url.netloc == host_url.netloc


def _safe_redirect_target(request: Request, target: str | None, default: str = "/") -> str:
    return target if _is_safe_redirect_target(target, request) else default


def _auth_error_redirect(message: str) -> RedirectResponse:
    return RedirectResponse(f"/?{urlencode({'auth_error': message})}", status_code=status.HTTP_303_SEE_OTHER)


def _set_authenticated_session(
    request: Request,
    user: Any,
    provider: str,
    id_token: str | None = None,
) -> None:
    csrf_token = request.session.get("_csrf_token")
    request.session.clear()
    if csrf_token:
        request.session["_csrf_token"] = csrf_token
    request.session["user_id"] = int(user["id"])
    request.session["auth_provider"] = provider
    if provider == "authentik" and id_token:
        request.session["authentik_id_token"] = id_token


def _session_out(request: Request, authenticated: bool, user: Any | None = None) -> SessionOut:
    return SessionOut(
        authenticated=authenticated,
        csrf_token=_get_csrf_token(request),
        user=_user_out(user) if user else None,
        authentik_enabled=AUTHENTIK_ENABLED,
        authentik_login_url=AUTHENTIK_LOGIN_PATH if AUTHENTIK_ENABLED else None,
        authentik_login_button_text=AUTHENTIK_LOGIN_BUTTON_TEXT,
        password_login_enabled=not AUTHENTIK_DISABLE_PASSWORD_LOGIN,
    )


async def require_csrf(request: Request) -> None:
    expected = request.session.get("_csrf_token")
    submitted = request.headers.get("X-CSRF-Token")
    if not expected or not submitted or not hmac.compare_digest(str(expected), str(submitted)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid CSRF token.")


def _user_out(user: Any) -> UserOut:
    return UserOut(id=int(user["id"]), email=str(user["email"]))


async def current_user(request: Request) -> Any:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
    user = db.get_user_by_id(int(user_id))
    if not user:
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
    return user


def _validate_alert_input(
    payload: WatchIn,
    user_id: int,
    exclude_watch_id: int | None = None,
    check_duplicate: bool = True,
) -> tuple[str, str, str]:
    company = payload.company_name.strip()
    url = payload.careers_url.strip()
    keywords = payload.keywords.strip()

    if not company:
        raise HTTPException(status_code=400, detail="Company name is required.")
    if len(company) > MAX_COMPANY_LENGTH:
        raise HTTPException(status_code=400, detail=f"Company name must be {MAX_COMPANY_LENGTH} characters or fewer.")
    if len(url) > MAX_URL_LENGTH:
        raise HTTPException(status_code=400, detail=f"Careers page URL must be {MAX_URL_LENGTH} characters or fewer.")
    if len(keywords) > MAX_KEYWORDS_LENGTH:
        raise HTTPException(status_code=400, detail=f"Keywords must be {MAX_KEYWORDS_LENGTH} characters or fewer.")

    safe_url, error = validate_public_http_url(url)
    if error:
        raise HTTPException(status_code=400, detail=error)

    if check_duplicate:
        duplicate = db.get_active_watch_by_url(user_id, safe_url, exclude_watch_id=exclude_watch_id)
        if duplicate:
            raise HTTPException(status_code=409, detail="You already have an active alert for that careers page.")

    return company, safe_url, keywords


def scan_diagnostic(error: str | None) -> dict[str, str] | None:
    text = str(error or "").strip()
    lower = text.lower()
    if not text:
        return None
    if "http 404" in lower:
        return {"title": "Page not found", "detail": "The careers URL returned HTTP 404. The company may have moved its jobs page."}
    if "http 403" in lower or "forbidden" in lower:
        return {"title": "Blocked by site", "detail": "The careers page refused the scraper request. This site may block automated checks."}
    if "http 429" in lower or "too many requests" in lower:
        return {"title": "Rate limited", "detail": "The site asked us to slow down. The next scheduled check may work."}
    if "only public careers page urls" in lower or "embedded usernames" in lower:
        return {"title": "URL blocked by safety checks", "detail": "Only public HTTP/HTTPS careers pages can be checked."}
    if "too many redirects" in lower:
        return {"title": "Redirect loop", "detail": "The careers URL redirected too many times before reaching a page."}
    if "greenhouse slug" in lower:
        return {"title": "Greenhouse board not found", "detail": "A Greenhouse board was detected, but Greenhouse did not recognize the board name."}
    if "lever slug" in lower:
        return {"title": "Lever board not found", "detail": "A Lever board was detected, but Lever did not recognize the board name."}
    if "unsupported ats" in lower:
        return {"title": "Unsupported job board", "detail": "This alert points at a job board the app does not know how to check yet."}
    if "could not fetch" in lower:
        return {"title": "Could not reach page", "detail": "The careers page could not be fetched. The URL, network, or site may be temporarily unavailable."}
    return {"title": "Scan failed", "detail": text}


def _job_ids(jobs: list[Any]) -> list[str]:
    return [str(job["job_id"]) for job in jobs]


def _notify_for_new_jobs(watch: Any, recipient_email: str, new_jobs: list[Any]) -> str:
    if not new_jobs:
        return "none"
    if not _email_enabled(watch):
        db.mark_jobs_notified(watch["id"], _job_ids(new_jobs))
        return "paused"
    sent = send_job_alert(recipient_email, watch["company_name"], new_jobs)
    if sent:
        db.mark_jobs_notified(watch["id"], _job_ids(new_jobs))
        return "sent"
    return "failed"


def _run_check(watch: Any) -> tuple[list[Any], int, str | None]:
    runnable = (
        (watch["ats_type"] in ("greenhouse", "lever") and watch["ats_slug"]) or
        (watch["ats_type"] == "custom" and watch["careers_url"])
    )
    if not runnable:
        return [], 0, "Not fully configured"

    jobs, error = check_watch(watch)
    if error:
        db.mark_watch_checked(watch["id"], error=error)
        return [], 0, error

    new_jobs = []
    for job in jobs:
        if db.save_job_if_new(watch["id"], job["job_id"], job["title"], job.get("location", ""), job.get("url", "")):
            new_jobs.append(job)

    expired = db.expire_old_jobs(watch["id"], [job["job_id"] for job in jobs])
    db.mark_watch_checked(watch["id"])
    return new_jobs, expired, None


def _run_checks_for_watches(watches: list[Any], source: str, fallback_email: str | None = None) -> dict[str, int]:
    stats = {"checked": 0, "new_jobs": 0, "expired": 0, "errors": 0, "email_failures": 0, "email_paused": 0}
    for watch in watches:
        stats["checked"] += 1
        logger.info("[%s] Checking %s", source, watch["company_name"])
        new_jobs, expired, error = _run_check(watch)
        stats["expired"] += expired
        if error:
            stats["errors"] += 1
            logger.warning("[%s] Skipped %s: %s", source, watch["company_name"], error)
            continue
        stats["new_jobs"] += len(new_jobs)
        if new_jobs:
            recipient = _row_value(watch, "user_email", fallback_email or "")
            notification = _notify_for_new_jobs(watch, recipient, new_jobs)
            if notification == "failed":
                stats["email_failures"] += 1
            elif notification == "paused":
                stats["email_paused"] += len(new_jobs)
    return stats


def run_all_checks() -> dict[str, int]:
    return _run_checks_for_watches(list(db.get_all_active_watches()), "scheduler")


def run_user_checks(user_id: int, user_email: str) -> dict[str, int]:
    return _run_checks_for_watches(list(db.get_watches_for_user(user_id)), f"user:{user_id}", user_email)


def _serialize_job(job: Any) -> JobOut:
    return JobOut(
        id=_row_value(job, "id"),
        watch_id=_row_value(job, "watch_id"),
        job_id=str(_row_value(job, "job_id", "")),
        title=str(_row_value(job, "title", "")),
        location=str(_row_value(job, "location", "") or ""),
        url=str(_row_value(job, "url", "") or ""),
        found_at=str(_row_value(job, "found_at", "") or "") or None,
        notified_at=str(_row_value(job, "notified_at", "") or "") or None,
        company_name=_row_value(job, "company_name"),
        keywords=_row_value(job, "keywords"),
    )


def _serialize_watch(watch: Any) -> WatchOut:
    jobs = list(db.get_jobs_for_watch(watch["id"]))
    error = _row_value(watch, "last_error")
    diagnostic = scan_diagnostic(error)
    return WatchOut(
        id=int(watch["id"]),
        company_name=str(watch["company_name"]),
        careers_url=str(watch["careers_url"] or ""),
        keywords=str(watch["keywords"] or ""),
        email_enabled=_email_enabled(watch),
        push_enabled=_push_enabled(watch),
        created_at=str(_row_value(watch, "created_at", "") or "") or None,
        last_checked=str(_row_value(watch, "last_checked", "") or "") or None,
        last_success_at=str(_row_value(watch, "last_success_at", "") or "") or None,
        last_error=str(error or "") or None,
        diagnostic=DiagnosticOut(**diagnostic) if diagnostic else None,
        job_count=len(jobs),
        jobs=[_serialize_job(job) for job in jobs[:5]],
    )


def _dashboard_for_user(user_id: int) -> DashboardOut:
    watches = [_serialize_watch(watch) for watch in db.get_watches_for_user(user_id)]
    return DashboardOut(
        watches=watches,
        stats=StatsOut(alerts=len(watches), jobs=sum(watch.job_count for watch in watches), interval=CHECK_INTERVAL),
    )


def _check_message(watch: Any, user_email: str, new_jobs: list[Any], expired: int, error: str | None) -> tuple[str, str]:
    company = watch["company_name"]
    if error:
        return f"Error checking {company}: {error}", "error"
    if new_jobs:
        notification = _notify_for_new_jobs(watch, user_email, new_jobs)
        count = len(new_jobs)
        if notification == "sent":
            return f"Found {count} new job{'s' if count != 1 else ''} at {company}. Check your email.", "success"
        if notification == "paused":
            return f"Found {count} new job{'s' if count != 1 else ''} at {company}. Email is paused, so they were saved without sending an alert.", "success"
        return f"Found {count} new job{'s' if count != 1 else ''} at {company}, but the email alert could not be sent.", "error"
    active_count = len(db.get_jobs_for_watch(watch["id"]))
    message = f"{company}: {active_count} current listing{'s' if active_count != 1 else ''}."
    if expired:
        message += f" {expired} stale listing{'s' if expired != 1 else ''} removed."
    return message, "info"


def _external_path(path: str) -> str:
    if APP_BASE_URL:
        return f"{APP_BASE_URL}{path}"
    return path


async def _authentik_logout_url(request: Request, id_token: str | None) -> str | None:
    if not AUTHENTIK_ENABLED:
        return None
    try:
        client = _get_authentik_client()
        metadata = getattr(client, "server_metadata", None) or {}
        if not metadata and hasattr(client, "load_server_metadata"):
            metadata = await client.load_server_metadata()
        end_session_endpoint = metadata.get("end_session_endpoint") if metadata else None
    except Exception:
        logger.exception("Could not load Authentik/OIDC logout metadata")
        return None

    if not end_session_endpoint:
        return None

    params = {"post_logout_redirect_uri": _external_url_for_path(request, "/")}
    if id_token:
        params["id_token_hint"] = id_token
    return f"{end_session_endpoint}?{urlencode(params)}"


def validate_startup_config() -> None:
    if not SECRET_KEY:
        message = "SECRET_KEY is not set; sessions will be invalidated on each container restart."
        if os.environ.get("REQUIRE_SECRET_KEY") == "1":
            raise RuntimeError(message)
        logger.warning(message)
    if AUTHENTIK_ENABLED:
        if OAuth is None:
            raise RuntimeError("AUTHENTIK_ENABLED requires authlib to be installed.")
        missing = _authentik_config_missing()
        if missing:
            raise RuntimeError(f"Missing Authentik/OIDC config: {', '.join(missing)}")
        logger.info(
            "Authentik/OIDC enabled; callback URL should be %s",
            _configured_external_url_for_path(AUTHENTIK_CALLBACK_PATH),
        )
    if not os.environ.get("SMTP_USER") or not os.environ.get("SMTP_PASS"):
        logger.warning("SMTP_USER/SMTP_PASS are not fully configured; email alerts will be retried until SMTP works.")


def start_scheduler() -> None:
    global _scheduler
    if os.environ.get("DISABLE_SCHEDULER") == "1":
        logger.info("Scheduler disabled by DISABLE_SCHEDULER=1")
        return
    if _scheduler and _scheduler.running:
        return
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(run_all_checks, "interval", hours=CHECK_INTERVAL, id="job_check", max_instances=1, coalesce=True)
    _scheduler.start()
    logger.info("Scheduler started; checking every %s hours", CHECK_INTERVAL)


@app.on_event("startup")
def bootstrap() -> None:
    validate_startup_config()
    db.init_db()
    start_scheduler()


@app.get("/api/session", response_model=SessionOut)
async def get_session(request: Request) -> SessionOut:
    user_id = request.session.get("user_id")
    user = db.get_user_by_id(int(user_id)) if user_id else None
    if not user:
        if user_id:
            request.session.clear()
        return _session_out(request, authenticated=False)
    return _session_out(request, authenticated=True, user=user)


@app.post("/api/auth/login", response_model=SessionOut, dependencies=[Depends(require_csrf)])
async def login(payload: LoginIn, request: Request) -> SessionOut:
    if AUTHENTIK_DISABLE_PASSWORD_LOGIN:
        raise HTTPException(status_code=403, detail="Password login is disabled. Use SSO to continue.")
    email = payload.email.strip().lower()
    if not _is_valid_email(email) or not payload.password:
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    user = db.get_user_by_email(email)
    if not user or not db.verify_password(user, payload.password):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    _set_authenticated_session(request, user, provider="local")
    return _session_out(request, authenticated=True, user=user)


@app.post("/api/auth/register", response_model=SessionOut, dependencies=[Depends(require_csrf)])
async def register(payload: RegisterIn, request: Request) -> SessionOut:
    if AUTHENTIK_DISABLE_PASSWORD_LOGIN:
        raise HTTPException(status_code=403, detail="Local account registration is disabled. Use SSO to continue.")
    email = payload.email.strip().lower()
    if not _is_valid_email(email):
        raise HTTPException(status_code=400, detail="Please provide a valid email address.")
    if not payload.password or len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    user, error = db.create_user(email, payload.password)
    if error:
        raise HTTPException(status_code=409, detail=error)
    _set_authenticated_session(request, user, provider="local")
    return _session_out(request, authenticated=True, user=user)


@app.get(AUTHENTIK_LOGIN_PATH)
async def authentik_login(request: Request) -> RedirectResponse:
    next_page = request.query_params.get("next")
    if request.session.get("user_id"):
        return RedirectResponse(_safe_redirect_target(request, next_page, "/"), status_code=status.HTTP_303_SEE_OTHER)
    if not AUTHENTIK_ENABLED:
        return _auth_error_redirect("SSO login is not configured.")

    request.session["authentik_next"] = _safe_redirect_target(request, next_page, "/")
    try:
        client = _get_authentik_client()
        redirect_uri = _external_url_for_path(request, AUTHENTIK_CALLBACK_PATH)
        return await client.authorize_redirect(request, redirect_uri)
    except Exception:
        logger.exception("Could not start Authentik/OIDC login")
        return _auth_error_redirect("Could not start SSO login. Check the Authentik configuration.")


@app.get(AUTHENTIK_CALLBACK_PATH)
async def authentik_callback(request: Request) -> RedirectResponse:
    if not AUTHENTIK_ENABLED:
        return _auth_error_redirect("SSO login is not configured.")

    try:
        client = _get_authentik_client()
        token = await client.authorize_access_token(request)
        userinfo = token.get("userinfo")
        if not userinfo:
            userinfo = await client.userinfo(token=token)
    except OAuthError:
        logger.exception("Authentik/OIDC callback failed")
        return _auth_error_redirect("SSO login failed. Please try again.")
    except Exception:
        logger.exception("Authentik/OIDC callback failed")
        return _auth_error_redirect("SSO login failed. Please try again.")

    user, error = _get_or_create_authentik_user(dict(userinfo))
    if error:
        return _auth_error_redirect(error)

    next_page = request.session.get("authentik_next")
    _set_authenticated_session(request, user, provider="authentik", id_token=token.get("id_token"))
    return RedirectResponse(_safe_redirect_target(request, next_page, "/"), status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/auth/logout", response_model=LogoutOut, dependencies=[Depends(require_csrf)])
async def logout(request: Request) -> LogoutOut:
    provider = request.session.get("auth_provider")
    id_token = request.session.get("authentik_id_token")
    logout_url = await _authentik_logout_url(request, id_token) if provider == "authentik" else None
    request.session.clear()
    return LogoutOut(logout_url=logout_url)


@app.get("/api/dashboard", response_model=DashboardOut)
async def dashboard(user: Any = Depends(current_user)) -> DashboardOut:
    return _dashboard_for_user(int(user["id"]))


@app.get("/api/jobs", response_model=list[JobOut])
async def jobs(user: Any = Depends(current_user)) -> list[JobOut]:
    return [_serialize_job(job) for job in db.get_recent_jobs_for_user(int(user["id"]), limit=200)]


@app.post("/api/preview", response_model=PreviewOut, dependencies=[Depends(require_csrf)])
async def preview(payload: WatchIn, user: Any = Depends(current_user)) -> PreviewOut:
    company, url, keywords = _validate_alert_input(payload, int(user["id"]), check_duplicate=False)
    watch = {"id": 0, "company_name": company, "careers_url": url, "ats_type": "custom", "ats_slug": None, "keywords": keywords}
    jobs, error = check_watch(watch)
    diagnostic = scan_diagnostic(error)
    return PreviewOut(
        company_name=company,
        careers_url=url,
        keywords=keywords,
        jobs=[_serialize_job(job) for job in jobs],
        error=error,
        diagnostic=DiagnosticOut(**diagnostic) if diagnostic else None,
    )


@app.post("/api/watches", response_model=ActionOut, dependencies=[Depends(require_csrf)])
async def create_watch(payload: WatchIn, user: Any = Depends(current_user)) -> ActionOut:
    company, url, keywords = _validate_alert_input(payload, int(user["id"]))
    watch_id = db.add_watch(int(user["id"]), company, url, "custom", None, keywords)
    watch = db.get_watch_for_user(watch_id, int(user["id"]))
    new_jobs, expired, error = _run_check(watch)
    message, category = _check_message(watch, str(user["email"]), new_jobs, expired, error)
    fresh = db.get_watch_for_user(watch_id, int(user["id"]))
    dashboard_data = _dashboard_for_user(int(user["id"]))
    return ActionOut(message=f"Added {company}. {message}", category=category, watch=_serialize_watch(fresh), stats=dashboard_data.stats)


@app.put("/api/watches/{watch_id}", response_model=ActionOut, dependencies=[Depends(require_csrf)])
async def update_watch(watch_id: int, payload: WatchIn, user: Any = Depends(current_user)) -> ActionOut:
    existing = db.get_watch_for_user(watch_id, int(user["id"]))
    if not existing:
        raise HTTPException(status_code=404, detail="Alert not found.")
    company, url, keywords = _validate_alert_input(payload, int(user["id"]), exclude_watch_id=watch_id)
    if not db.update_watch(watch_id, int(user["id"]), company, url, keywords):
        raise HTTPException(status_code=404, detail="Alert not found.")
    watch = db.get_watch_for_user(watch_id, int(user["id"]))
    new_jobs, expired, error = _run_check(watch)
    message, category = _check_message(watch, str(user["email"]), new_jobs, expired, error)
    fresh = db.get_watch_for_user(watch_id, int(user["id"]))
    dashboard_data = _dashboard_for_user(int(user["id"]))
    return ActionOut(message=f"Updated {company}. {message}", category=category, watch=_serialize_watch(fresh), stats=dashboard_data.stats)


@app.delete("/api/watches/{watch_id}", response_model=ActionOut, dependencies=[Depends(require_csrf)])
async def delete_watch(watch_id: int, user: Any = Depends(current_user)) -> ActionOut:
    watch = db.get_watch_for_user(watch_id, int(user["id"]))
    if not watch:
        raise HTTPException(status_code=404, detail="Alert not found.")
    db.delete_watch(watch_id, int(user["id"]))
    dashboard_data = _dashboard_for_user(int(user["id"]))
    return ActionOut(message=f"Removed {watch['company_name']}.", category="success", watches=dashboard_data.watches, stats=dashboard_data.stats)


@app.patch("/api/watches/{watch_id}/notifications", response_model=ActionOut, dependencies=[Depends(require_csrf)])
async def update_notifications(watch_id: int, payload: NotificationIn, user: Any = Depends(current_user)) -> ActionOut:
    watch = db.get_watch_for_user(watch_id, int(user["id"]))
    if not watch:
        raise HTTPException(status_code=404, detail="Alert not found.")
    if not db.set_watch_notification_settings(watch_id, int(user["id"]), payload.email_enabled, payload.push_enabled):
        raise HTTPException(status_code=500, detail="Could not update notification settings.")
    fresh = db.get_watch_for_user(watch_id, int(user["id"]))
    return ActionOut(message=f"Notification settings saved for {fresh['company_name']}.", category="success", watch=_serialize_watch(fresh))


@app.post("/api/watches/{watch_id}/check", response_model=ActionOut, dependencies=[Depends(require_csrf)])
async def check_one(watch_id: int, user: Any = Depends(current_user)) -> ActionOut:
    watch = db.get_watch_for_user(watch_id, int(user["id"]))
    if not watch:
        raise HTTPException(status_code=404, detail="Alert not found.")
    new_jobs, expired, error = _run_check(watch)
    message, category = _check_message(watch, str(user["email"]), new_jobs, expired, error)
    fresh = db.get_watch_for_user(watch_id, int(user["id"]))
    dashboard_data = _dashboard_for_user(int(user["id"]))
    return ActionOut(message=message, category=category, watch=_serialize_watch(fresh), stats=dashboard_data.stats)


@app.post("/api/check-now", response_model=ActionOut, dependencies=[Depends(require_csrf)])
async def check_all(user: Any = Depends(current_user)) -> ActionOut:
    stats = run_user_checks(int(user["id"]), str(user["email"]))
    message = f"Check complete: {stats['checked']} alert{'s' if stats['checked'] != 1 else ''} checked."
    if stats["new_jobs"]:
        message += f" {stats['new_jobs']} new listing{'s' if stats['new_jobs'] != 1 else ''} found."
    if stats["expired"]:
        message += f" {stats['expired']} stale listing{'s' if stats['expired'] != 1 else ''} removed."
    if stats["email_paused"]:
        message += f" {stats['email_paused']} listing{'s' if stats['email_paused'] != 1 else ''} saved without email."
    category = "success"
    if stats["email_failures"]:
        message += " Some email alerts could not be sent and will be retried."
        category = "error"
    elif stats["errors"]:
        message += " Some alerts could not be checked."
        category = "info"
    dashboard_data = _dashboard_for_user(int(user["id"]))
    return ActionOut(message=message, category=category, watches=dashboard_data.watches, stats=dashboard_data.stats)


@app.post("/api/watches/reorder", response_model=ActionOut, dependencies=[Depends(require_csrf)])
async def reorder(payload: ReorderIn, user: Any = Depends(current_user)) -> ActionOut:
    if not db.reorder_watches(int(user["id"]), payload.watch_ids):
        raise HTTPException(status_code=400, detail="Could not save that alert order.")
    dashboard_data = _dashboard_for_user(int(user["id"]))
    return ActionOut(message="Alert order saved.", category="success", watches=dashboard_data.watches, stats=dashboard_data.stats)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/manifest.webmanifest")
async def manifest() -> FileResponse:
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker() -> FileResponse:
    response = FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/offline")
async def offline() -> FileResponse:
    return FileResponse(STATIC_DIR / "offline.html", media_type="text/html")


@app.get("/{full_path:path}")
async def serve_app(full_path: str) -> FileResponse:
    requested = (DIST_DIR / full_path).resolve()
    if DIST_DIR.exists() and str(requested).startswith(str(DIST_DIR.resolve())) and requested.is_file():
        return FileResponse(requested)
    index = DIST_DIR / "index.html"
    if index.exists():
        return FileResponse(index, media_type="text/html")
    raise HTTPException(status_code=404, detail="Frontend has not been built.")
