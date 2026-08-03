# SRL Backend — Documentation

**SRL** (Spaced Repetition Learning) is a multi-user flashcard trainer with FSRS
scheduling. This is the API service (`SRL-BE`); the web client lives in `SRL-FE`.

Cards are authored in a rich-text editor and stored as ProseMirror/TipTap JSON. Every
user signs in (social or email/password), gets a JWT, and sees only their own cards,
schedule, review history, and trained FSRS weights.

## Contents

| Doc | What's in it |
|-----|--------------|
| [architecture.md](architecture.md) | Components, request flow, tech stack, module map |
| [authentication.md](authentication.md) | Social + email/password login, JWT, admin, provider setup |
| [data-model.md](data-model.md) | Tables, per-user scoping, migrations, orphan claiming |
| [api-reference.md](api-reference.md) | Every endpoint, auth requirements, payloads |
| [spaced-repetition.md](spaced-repetition.md) | FSRS scheduling and the per-user optimizer |
| [deployment.md](deployment.md) | Railway deploy, all env vars, first-run checklist |
| [development.md](development.md) | Local setup, running, and the test suites |

## 30-second overview

- **Stack:** FastAPI · SQLModel/SQLAlchemy 2 · PostgreSQL (psycopg 3) · py-fsrs · `uv`.
- **Auth:** OAuth2 (Google/GitHub/Yandex/VK) or email+password → app-signed JWT (HS256),
  sent as `Authorization: Bearer` on every call.
- **Isolation:** `user_id` on cards/progress/reviews/fsrs_params; all queries scoped.
- **Limits:** 1000 new cards / 24h / user. Admin-only endpoints gated by `ADMIN_EMAILS`.
- **GitBook:** the importer is gone; `/asset` remains to serve already-imported images.

> **Keep this current:** update the relevant page in `docs/` in the *same commit* as any
> change to endpoints, the data model, auth, env vars, or deploy steps.
