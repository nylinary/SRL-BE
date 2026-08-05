# Data Model

PostgreSQL via SQLModel. Tables are created by `SQLModel.metadata.create_all`; additive
column changes are applied by `run_migrations` (see below). All content/scheduling tables
carry `user_id` and every query is scoped to it.

## Tables (`gitbook/models.py`)

### `users`
| Column | Type | Notes |
|--------|------|-------|
| `id` | str (PK) | uuid hex |
| `email` | str? | indexed; stored **canonical** (see auth docs); may be null if a provider returns none |
| `name`, `avatar_url` | str | profile display |
| `provider` | str | last provider used, or `"password"` |
| `password_hash` | str? | set only for email/password accounts (bcrypt) |
| `is_admin` | bool | recomputed from `ADMIN_EMAILS` each login |
| `created_at`, `last_login` | float | epoch seconds |

### `oauth_accounts`
Links a social identity to a user, so one user can attach several providers.
`(provider, subject)` is the lookup key. Columns: `id` (PK), `provider`, `subject`,
`user_id`, `created_at`.

### `cards`
The authored content. `question`/`answer` are **ProseMirror/TipTap JSON** documents
(JSONB). Columns: `id` (PK, uuid), **`user_id`** (indexed), `question`, `answer`,
`theme` (indexed), `subtheme`, `tags` (JSONB), `position`, `created_at` (indexed),
`updated_at` (indexed), `source_extract_id` (indexed, nullable) — the
`reading_items` extract a card was made from, or `NULL` if authored directly.

### `reading_items`
Incremental-reading library: a per-user self-referential tree of documents and extracts
(`reading.py`). A **document** (`kind="document"`, `parent_id=NULL`) holds imported TXT/PDF
text; an **extract** (`kind="extract"`) is a selection lifted from its parent and nests
arbitrarily. Columns: `id` (PK, uuid), **`user_id`** (indexed), `parent_id` (indexed,
nullable), `kind`, `title`, `content` (`TEXT`), `source_kind` (`'text'`/`'pdf'`, documents
only), `position` (indexed, sort key), `created_at`, `updated_at`. Deleting an item removes
its whole subtree. See `docs/reading.md`.

### `reading_blobs`
The original bytes of an uploaded PDF, so the client can render the document as-is (rather
than showing server-extracted text). Kept in a **separate table** — not a column on
`reading_items` — so the tree/content queries never haul megabytes of binary. Columns:
`item_id` (PK = the document's id), `data` (`BYTEA`). One row per PDF document; deleted
together with its item.

### `progress`
One row per card = its FSRS scheduling state. `question_id` (PK) = the card id.
`card_json` is the serialised `fsrs.Card`; `due`/`state` are denormalised for the picker;
`reps`/`rating_sum`/`last_rating` back the stats aggregates. Plus **`user_id`** and cached
`theme/subtheme/section/question_text`.

### `reviews`
Append-only grade log: `id` (PK), **`user_id`**, `question_id`, `rating` (1–4),
`reviewed_at`. Feeds stats and the optimizer.

### `fsrs_params`
Trained weight sets, newest-per-user wins: `id` (PK), **`user_id`**, `weights` (JSONB, 21
floats), `desired_retention`, `review_count`, `trained_at`.

## Per-user scoping

- `content.py::CardRepository` — every method takes `user_id`; `get/update/delete` also
  verify ownership and return `None`/`False` cross-user. `created_since(user_id, epoch)`
  backs the daily limit.
- `gitbook/store.py::ReviewStore` — every read/write is filtered by `user_id`. Schedulers
  are built from *that user's* latest `fsrs_params` and cached per user; `save_weights`
  invalidates the cache so new weights apply with no restart.
- `gitbook/optimizer.py::OptimizerService` — `status(user_id)` / `run(user_id)`.

A user therefore never sees another user's cards, schedule, history, or weights.

## Migrations

`make_engine()` runs, in order:
1. `SQLModel.metadata.create_all(engine)` — creates any missing tables (`users`,
   `oauth_accounts`, …).
2. `run_migrations(engine)` — idempotent `ALTER TABLE … ADD COLUMN IF NOT EXISTS` and
   `CREATE INDEX IF NOT EXISTS`, safe to run on every boot. This is what adds `user_id`
   (and `users.password_hash`) to databases created before those columns existed.

There is no Alembic; the additive-only list in `models.py::_MIGRATIONS` is the migration
history. To evolve the schema, add an idempotent statement there.

> **Adding a column?** Add the field to the model **and** an `ADD COLUMN IF NOT EXISTS`
> line to `_MIGRATIONS`, or existing deployments won't get it.

## Orphan rows & claiming

When the app first migrated from single-user, existing `cards/progress/reviews/
fsrs_params` had no owner, so they get `user_id = ''` ("orphans"). They belong to nobody
until claimed:

- `UserRepository.claim_orphans(user_id)` sets `user_id` on all empty-owner rows to that
  user and returns per-table counts.
- It runs **automatically** whenever an admin logs in (`main._session_for`), and is also
  exposed as `POST /api/admin/claim-orphans` for a manual re-run. Idempotent.

**Dangling history (removed from source).** A `progress` row whose `question_id` no longer
matches any card shows as "removed from source" in stats. This happens when cards are
wiped and re-created (e.g. a `?replace=true` re-import) — history keyed by the old ids is
left behind. `CardRepository.restore_orphaned(user_id)` (endpoint
`POST /api/admin/restore-orphaned`, and a button on the stats screen) rebuilds a card for
each such row **reusing the old `question_id`** so the history reconnects. It's additive
(never edits existing cards) and skips history whose question text already exists as a
card. Answers can't be recovered — only the question text survived — so restored cards
start empty.
