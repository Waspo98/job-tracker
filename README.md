# Job Tracker

A self-hosted job alert dashboard for watching company careers pages and surfacing matching job listings.

This branch rebuilds the app around a React frontend and a FastAPI JSON backend. The previous server-rendered dashboard and card-fragment refresh system have been removed from the main interaction path.

## Architecture

- React + TypeScript + Vite frontend
- FastAPI backend with session auth and CSRF-protected mutations
- SQLite persistence
- APScheduler background checks
- Greenhouse, Lever, and custom careers-page scanning
- Gmail SMTP alerts when configured
- Docker image that builds the frontend and serves it from the API container

## Quick Start

1. Copy the example environment file.

```bash
cp .env.example .env
```

2. Edit `.env`.

```env
SECRET_KEY=change-me-to-a-long-random-value
REQUIRE_SECRET_KEY=1
CHECK_INTERVAL_HOURS=4
SMTP_USER=yourgmail@gmail.com
SMTP_PASS=your-gmail-app-password
APP_BASE_URL=https://jobs.example.com
LOG_LEVEL=INFO
```

3. Start the app.

```bash
docker compose up -d --build
```

4. Visit the app.

```text
http://localhost:5055
```

## Development

The production Docker build runs:

```text
frontend package install -> Vite build -> FastAPI runtime image
```

For backend tests:

```bash
docker run --rm \
  -e DISABLE_SCHEDULER=1 \
  -e SECRET_KEY=test-secret-key \
  -v "$PWD:/src" \
  -w /src \
  job-tracker:local \
  python -m unittest discover -s tests
```

For frontend development outside Docker, install dependencies in `frontend/` and run Vite:

```bash
cd frontend
npm install
npm run dev
```

Vite proxies `/api`, `/static`, `/sw.js`, and related app routes to `http://127.0.0.1:5055`.

## Email Setup

For Gmail alerts, create an app password:

1. Enable 2-Step Verification on the Google account.
2. Create an app password for "Job Tracker".
3. Put the generated password in `SMTP_PASS`.

If SMTP is missing or fails, discovered jobs remain pending for notification and the app retries on future checks.

## Push Notification Framework

Browser push delivery remains staged. The UI and database keep per-alert push preferences, and the service worker can display push payloads. The remaining pieces are:

1. Add VAPID public/private keys to the environment.
2. Add a browser subscription endpoint that saves `endpoint`, `p256dh`, and `auth`.
3. Send Web Push payloads when push is enabled for an alert.

## Notes

- Authentik/OIDC from the old Flask app is not wired into this rebuild yet.
- Existing SQLite data is intended to continue working with the current schema.
- The legacy Jinja templates remain in the tree for comparison during the migration, but the new runtime serves the React app.
