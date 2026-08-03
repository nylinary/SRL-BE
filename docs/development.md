# Local Development

## Setup

```bash
cd backend
uv sync                       # or: uv sync --extra optimizer  (for weight training)
cp .env.example .env          # then fill in the values below
```

Minimum `.env` to boot:

```
DATABASE_URL=postgresql://postgres@localhost:5432/srl
JWT_SECRET=<any long random string>
ADMIN_EMAILS=you@example.com
PUBLIC_BACKEND_URL=http://localhost:8000
FRONTEND_URL=http://localhost:5173
```

Email/password login works with just those. For social login locally, add a provider's
`*_CLIENT_ID`/`*_CLIENT_SECRET` and register its callback as
`http://localhost:8000/api/auth/<provider>/callback`.

## Run

```bash
uv run uvicorn main:app --reload --port 8000
# Swagger UI: http://localhost:8000/docs
```

## Tests

```bash
uv run python tests/test_auth.py        # JWT + OAuth URL building (no DB)
uv run python tests/test_content.py     # ProseMirror render / html_to_doc (no DB)
uv run python tests/test_parser.py      # GitBook markdown parser (no DB)

# Store + per-user isolation (needs Postgres; targets a *_test DB, else SKIPs):
TEST_DATABASE_URL=postgresql://postgres@localhost:5432/qt_test \
  uv run python tests/test_store.py
```

`tests/test_store.py` recreates its tables and refuses to run against a DB whose name
doesn't contain `test`.

## Common tasks

- **Add an endpoint** → `main.py`; add a Bearer dep (`get_current_user`) unless it's truly
  public; update [api-reference.md](api-reference.md).
- **Add a table column** → the model in `gitbook/models.py` **and** an
  `ADD COLUMN IF NOT EXISTS` line in `_MIGRATIONS`; update [data-model.md](data-model.md).
- **Add an OAuth provider** → extend `auth/oauth.py` (`SUPPORTED`, `authorize_url`, an
  `_exchange` branch) and `OAUTH_PROVIDERS` in `gitbook/config.py`; update
  [authentication.md](authentication.md).

## Documentation discipline

Update the relevant `docs/` page in the **same commit** as any change to endpoints, the
data model, auth, env vars, deploy steps, or FSRS behaviour. The docs are the contract.
