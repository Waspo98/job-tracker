# Job Tracker

A self-hosted job alert dashboard for watching company careers pages and surfacing matching job listings.

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

2. Generate a real session secret and put it in `.env`.

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

At minimum, replace `SECRET_KEY` before starting the app:

```env
SECRET_KEY=paste-your-generated-secret-here
REQUIRE_SECRET_KEY=1
REGISTRATION_MODE=first-user-only
JOB_TRACKER_PORT=5055
DATABASE_PATH=/app/data/jobs.db
CHECK_INTERVAL_HOURS=4
SMTP_USER=yourgmail@gmail.com
SMTP_PASS=your-gmail-app-password
APP_BASE_URL=https://jobs.example.com
SESSION_COOKIE_SECURE=1
TRUSTED_HOSTS=jobs.example.com
AUTHENTIK_ENABLED=0
VAPID_PUBLIC_KEY=
VAPID_PRIVATE_KEY=
VAPID_SUBJECT=mailto:admin@example.com
LOG_LEVEL=INFO
```

3. Start the app.

```bash
docker compose up -d --build
```

4. Visit the app and create the first local account.

```text
http://localhost:5055
```

The default compose file only needs Docker and a local `./data` directory. If you use a reverse proxy, attach the service to your proxy network in a compose override file rather than editing the default install path.

To use a published image instead of building locally, set `JOB_TRACKER_IMAGE` to the GitHub Container Registry image for your fork or upstream repo:

```env
JOB_TRACKER_IMAGE=ghcr.io/OWNER/REPOSITORY:latest
```

Then run:

```bash
docker compose pull
docker compose up -d
```

## Public Self-Host Checklist

- Replace `SECRET_KEY`; the container refuses to boot with the example value while `REQUIRE_SECRET_KEY=1`.
- Keep `REGISTRATION_MODE=first-user-only` unless you intentionally want open signups.
- Put the public origin in `APP_BASE_URL` when serving behind a proxy.
- Set `SESSION_COOKIE_SECURE=1` for HTTPS deployments.
- Set `TRUSTED_HOSTS` to the public host name when the app is internet-facing.
- Back up `./data/jobs.db`; it contains users, alerts, discovered jobs, and push subscriptions.

## Reverse Proxy Example

Use `docker-compose.proxy-example.yml` as a starting point when Traefik, Caddy, Nginx Proxy Manager, or another proxy owns the public port.

```bash
cp docker-compose.proxy-example.yml docker-compose.proxy.yml
# Edit image name, proxy network name, and any proxy labels your setup needs.
docker compose -f docker-compose.proxy.yml up -d
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

## Publishing Images

The GitHub Actions workflow publishes to GHCR on branch pushes and version tags when package permissions are enabled for the repository:

- `ghcr.io/OWNER/REPOSITORY:latest` for the default branch
- `ghcr.io/OWNER/REPOSITORY:beta` for the `beta` branch
- `ghcr.io/OWNER/REPOSITORY:vX.Y.Z` for version tags
- `ghcr.io/OWNER/REPOSITORY:sha-...` for exact commit images

## Email Setup

For Gmail alerts, create an app password:

1. Enable 2-Step Verification on the Google account.
2. Create an app password for "Job Tracker".
3. Put the generated password in `SMTP_PASS`.

If SMTP is missing or fails, discovered jobs remain pending for notification and the app retries on future checks.

## Authentik / OIDC Setup

Email/password auth remains enabled by default. To add Authentik SSO, create an Authentik OAuth2/OpenID provider and application, then set:

```env
APP_BASE_URL=https://jobs.example.com
AUTHENTIK_ENABLED=1
AUTHENTIK_ISSUER_URL=https://auth.example.com/application/o/job-tracker/
AUTHENTIK_CLIENT_ID=your-client-id
AUTHENTIK_CLIENT_SECRET=your-client-secret
AUTHENTIK_LOGIN_BUTTON_TEXT=Log in with your SSO account
AUTHENTIK_SCOPES=openid email profile
AUTHENTIK_AUTO_REGISTER=1
AUTHENTIK_REQUIRE_VERIFIED_EMAIL=1
AUTHENTIK_DISABLE_PASSWORD_LOGIN=0
```

With `AUTHENTIK_AUTO_REGISTER=1`, first-time Authentik users are created automatically after a successful OIDC callback. If `AUTHENTIK_REQUIRE_VERIFIED_EMAIL=1`, Authentik must include an `email_verified=true` claim for the user; otherwise Job Tracker redirects back to login and refuses to create the local account.

Configure the Authentik redirect URI as:

```text
https://jobs.example.com/auth/authentik/callback
```

`AUTHENTIK_DISABLE_PASSWORD_LOGIN=1` hides and disables local email/password login, but the default keeps local login and registration available. `OIDC_*` aliases are also accepted for issuer, client ID, client secret, scopes, login button text, and enablement.

## Browser Push Notifications

Browser push notifications use VAPID keys and the app service worker. Generate one keypair, keep it stable in `.env`, then rebuild/restart the container.

```bash
docker run --rm node:22-slim node -e "const crypto=require('crypto'); const {privateKey,publicKey}=crypto.generateKeyPairSync('ec',{namedCurve:'prime256v1'}); const jwkPriv=privateKey.export({format:'jwk'}); const jwkPub=publicKey.export({format:'jwk'}); const b=(s)=>Buffer.from(s,'base64url'); const pub=Buffer.concat([Buffer.from([4]),b(jwkPub.x),b(jwkPub.y)]).toString('base64url'); console.log('VAPID_PUBLIC_KEY='+pub); console.log('VAPID_PRIVATE_KEY='+jwkPriv.d);"
```

```env
VAPID_PUBLIC_KEY=copy-public-key
VAPID_PRIVATE_KEY=copy-private-key
VAPID_SUBJECT=mailto:admin@example.com
```

After that, open an alert's **Notifications** menu, enable **Browser push**, and approve the browser permission prompt on each device that should receive alerts. Push is per alert: the app only sends browser notifications for alerts where Browser push is enabled.

## Notes

- Authentik/OIDC uses the same callback path as the old Flask app.
- Existing SQLite data is intended to continue working with the current schema.
- The legacy Jinja templates remain in the tree for comparison during the migration, but the new runtime serves the React app.
