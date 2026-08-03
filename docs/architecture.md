# Architecture

## Tech stack

- **FastAPI** — HTTP API (`main.py`).
- **SQLModel** on **SQLAlchemy 2** with **psycopg 3** — PostgreSQL access.
- **py-fsrs** — the FSRS spaced-repetition model and optimizer.
- **PyJWT** — session + OAuth-state tokens. **bcrypt** — password hashing. **httpx** —
  OAuth provider calls.
- **uv** — dependency and virtualenv management (Python 3.11–3.12; the optimizer needs
  torch, which has no 3.13 wheels yet).

## Module map

```
backend/
├── main.py                # FastAPI app: routes, request/response shaping, wiring
├── content.py             # ProseMirror render + CardRepository (per-user CRUD)
├── auth/
│   ├── tokens.py          # issue/read our session JWT and the OAuth state token
│   ├── oauth.py           # provider configs + code→profile exchange (4 providers)
│   ├── passwords.py       # bcrypt hash/verify
│   ├── users.py           # UserRepository: upsert, register, authenticate, claim_orphans
│   └── deps.py            # get_current_user / require_admin FastAPI dependencies
├── gitbook/               # legacy package name; now the app core
│   ├── config.py          # Settings.from_env() — all configuration
│   ├── models.py          # SQLModel tables + engine factory + idempotent migrations
│   ├── store.py           # ReviewStore: per-user FSRS state + scheduler cache
│   ├── optimizer.py       # OptimizerService: per-user weight training
│   ├── render.py          # GitBook markdown → HTML (used only by /asset-era content)
│   └── source.py          # GitLab asset proxy (backs /asset)
└── tests/                 # test_auth, test_content, test_parser, test_store
```

> The `gitbook/` package keeps its name for git history; it is the application core, not
> GitBook-specific. Only `source.py` (asset proxy) and `render.py` still relate to GitBook.

## Startup wiring (`main.py`)

```
settings = get_settings()                 # env → Settings (cached)
store    = open_store(settings)           # engine + create_all + run_migrations
cards    = CardRepository(store.engine)
optimizer= OptimizerService(store, settings)
users    = UserRepository(store.engine)
bind_users(users)                         # makes deps.get_current_user work
```

`open_store` runs `SQLModel.metadata.create_all` (new tables) and then `run_migrations`
(additive `ALTER TABLE … ADD COLUMN IF NOT EXISTS` for databases that predate a column).
See [data-model.md](data-model.md).

## Request flow (an authenticated call)

```
Client                         FastAPI                         PostgreSQL
  │  GET /api/cards               │                                  │
  │  Authorization: Bearer <jwt>  │                                  │
  │──────────────────────────────▶│ get_current_user:               │
  │                               │  read_session(jwt) → user_id     │
  │                               │  users.get(user_id) ─────────────▶│
  │                               │◀──────────────── User            │
  │                               │ cards.list(user.id, …) ──────────▶│
  │                               │◀──────────── rows (user's only)  │
  │◀───────────── 200 {cards:[…]} │                                  │
```

Anything without a valid Bearer token gets `401`; the client then drops the session and
shows the login screen.

## Login flow (OAuth)

```
Browser → GET /api/auth/{provider}/login
        → 302 to provider authorize URL (signed `state`)
Provider→ 302 back to /api/auth/{provider}/callback?code&state
Backend → verify state · exchange code → profile · upsert user · issue JWT
        → 302 to {FRONTEND_URL}/#/auth/callback?token=<jwt>
Browser → stores token, calls /api/auth/me, enters the app
```

Email/password skips the provider hops: `POST /api/auth/register` or `/api/auth/login`
returns `{token, user}` directly. See [authentication.md](authentication.md).

## Concurrency

Each graded review takes a per-card Postgres advisory lock (`pg_advisory_xact_lock`) so
the read-modify-write around the FSRS card is atomic across workers. Per-user schedulers
are cached in-process behind a lock and rebuilt lazily after training.
