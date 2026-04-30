# Job Tracker

A small self-hosted Flask app for watching company careers pages and emailing new matching job listings.

It stores users, watches, and discovered jobs in SQLite. The active flow is intentionally simple: paste a public careers page URL, add optional title keywords, and let the scheduled scraper check it every few hours.

## Features

- Per-user job watches
- Edit saved watches and re-check immediately
- Keyword filtering by job title
- SQLite persistence
- Email alerts through Gmail SMTP/app passwords
- Manual per-user checks and scheduled checks
- Greenhouse and Lever board detection on custom careers pages
- CSRF protection for state-changing forms
- Public URL validation for custom scraper targets
- Docker image suitable for GitHub Container Registry

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

## Email Setup

For Gmail alerts, create an app password:

1. Enable 2-Step Verification on the Google account.
2. Create an app password for "Job Tracker".
3. Put the generated password in `SMTP_PASS`.

If SMTP is missing or fails, the app keeps discovered jobs pending for notification and retries on future checks.

## Running Tests

```bash
python -m unittest discover -s tests
```

If Python is not installed locally, run the tests by bind-mounting the repo into the image:

```bash
docker build -t job-tracker:test .
docker run --rm \
  -e DISABLE_SCHEDULER=1 \
  -e SECRET_KEY=test-secret-key \
  -v "$PWD:/src" \
  -w /src \
  job-tracker:test \
  python -m unittest discover -s tests
```

The production image does not include the test directory. In GitHub Actions, tests run before the container publish job.

## GitHub Container Registry

This repo includes a GitHub Actions workflow that:

- runs the test suite
- builds the Docker image
- publishes `ghcr.io/<owner>/<repo>:latest` from `main`
- publishes `ghcr.io/<owner>/<repo>:beta` from `beta`
- publishes version tags from `v*` Git tags

The workflow uses `GITHUB_TOKEN` with `packages: write` permission. After the first publish, set package visibility in GitHub if you want the image public.

To run a published image:

```bash
docker run -d \
  --name job-tracker \
  --restart unless-stopped \
  --env-file .env \
  -p 5055:5055 \
  -v job-tracker-data:/app/data \
  ghcr.io/<owner>/<repo>:latest
```

To try the beta channel:

```bash
docker run -d \
  --name job-tracker-beta \
  --restart unless-stopped \
  --env-file .env \
  -p 5056:5055 \
  -v job-tracker-beta-data:/app/data \
  ghcr.io/<owner>/<repo>:beta
```

## Updating

```bash
docker compose down
docker compose up -d --build
```

## Notes

- Only public HTTP/HTTPS careers page URLs are accepted for custom watches.
- Custom watches first look for supported Greenhouse/Lever board links, then fall back to static HTML scraping.
- Keep `.env` and `data/` out of Git. The included ignore files already do this.
