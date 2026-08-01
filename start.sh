#!/usr/bin/env bash
# Railway start script.
# In the Railway service settings set the Start Command to:  bash start.sh
# Required env vars: DATABASE_URL (Railway Postgres plugin), GITLAB_TOKEN. Railway sets PORT.
set -euo pipefail

# uv isn't in Railway's base image — install it on first boot if missing.
if ! command -v uv >/dev/null 2>&1; then
  echo "› installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# Install locked deps + the optimizer extra (torch) on the pinned interpreter.
# uv provisions Python 3.12 itself (per .python-version), so the base image's
# Python version doesn't matter. --frozen = use uv.lock exactly, no re-resolving.
echo "› uv sync --extra optimizer"
uv sync --extra optimizer --frozen

# DATABASE_URL from the Postgres plugin is postgresql://… — the app normalises it to
# the psycopg driver. Tables are created on first run. Railway provides $PORT.
echo "› starting uvicorn on :${PORT:-8000}"
exec uv run uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
