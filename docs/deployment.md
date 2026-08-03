# Deployment

Deployed on **Railway** (backend + frontend as separate services, plus a Postgres
plugin). The backend is API-only; the frontend is a separate origin that calls it.

- `build.buildCommand`: `uv sync --extra optimizer --locked --no-dev`
- `deploy.startCommand`: `uvicorn main:app --host 0.0.0.0 --port $PORT`

## Environment variables

### Required
| Var | Purpose |
|-----|---------|
| `DATABASE_URL` | PostgreSQL DSN. Tables + migrations run on boot. |
| `JWT_SECRET` | Signs session + OAuth-state tokens. Generate: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `ADMIN_EMAILS` | Comma-separated admin emails (re-checked each login). |
| `PUBLIC_BACKEND_URL` | This API's public origin — builds OAuth `redirect_uri`. |
| `FRONTEND_URL` | Where the OAuth callback bounces the browser back to. |

### Auth / limits (optional, with defaults)
| Var | Default | Purpose |
|-----|---------|---------|
| `JWT_TTL_DAYS` | `30` | Session lifetime. |
| `DAILY_CARD_LIMIT` | `1000` | Max new cards / 24h / user (`0` = unlimited). |
| `CORS_ORIGINS` | `*` | Comma-separated allowed origins for the API. |

### OAuth providers (each optional; a provider shows only when both are set)
`GOOGLE_CLIENT_ID` · `GOOGLE_CLIENT_SECRET` · `GITHUB_CLIENT_ID` · `GITHUB_CLIENT_SECRET` ·
`YANDEX_CLIENT_ID` · `YANDEX_CLIENT_SECRET`.
Register each app with redirect URI `{PUBLIC_BACKEND_URL}/api/auth/{provider}/callback` —
see [authentication.md](authentication.md#registering-the-oauth-apps).

### FSRS (optional)
`FSRS_RETENTION` (0.9) · `FSRS_MAX_INTERVAL` (36500) · `FSRS_ENABLE_FUZZ` (true) ·
`FSRS_PARAMETERS` (blank = library defaults). See [spaced-repetition.md](spaced-repetition.md).

### GitBook `/asset` (optional, legacy)
`GITLAB_TOKEN`, `GITLAB_URL`, `GITLAB_PROJECT`, `GITLAB_REF`, `GITBOOK_FILE` — only needed
if imported cards still reference images served through `/asset`.

`.env.example` in the repo root documents all of these for local runs.

## First-run checklist (new deployment)

1. Set the **Required** vars + at least one login method (a provider pair, or nothing extra
   for email/password which works out of the box).
2. Deploy. On boot the schema is created and migrations run (idempotent).
3. Sign in with an address listed in `ADMIN_EMAILS` (email/password is easiest to start).
   As an admin, your login **auto-claims** any pre-existing single-user data into your
   account — no manual step. (Manual re-run: `POST /api/admin/claim-orphans` with your
   Bearer token, found in the browser's `localStorage['srl:token']`.)
4. On the frontend service, set `VITE_API_URL` to this backend's origin and redeploy — see
   the frontend `docs/deployment.md`.

## Upgrades

Additive migrations run automatically on each boot. When you add a DB column, also add an
`ADD COLUMN IF NOT EXISTS` line to `_MIGRATIONS` (see [data-model.md](data-model.md)).
No manual migration command is required.
