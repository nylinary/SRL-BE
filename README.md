# Question Trainer

Flashcard-style drilling over **your own cards**, scheduled with
**[FSRS](https://github.com/open-spaced-repetition/py-fsrs)**. You author question/answer
cards in a rich-text editor; the trainer shows one at a time with the answer collapsed,
and grading each recall feeds FSRS, which decides when you see the card again.

Two front-ends over one FastAPI + PostgreSQL backend:

- **Study app** (`/`) — the existing vanilla-JS trainer: Тренировка / Статистика / Настройки.
- **Card manager** (`frontend/`, React + Vite + TipTap, served at `/manage`) — browse,
  create, and edit cards.

## Cards & content

A card is a `question` and an `answer`, each a **ProseMirror/TipTap JSON document** —
the one portable rich-text format shared across every client (web now; a browser
extension and React Native iOS/Android later). Supported: headings, bold, italic, inline
code, bullet/ordered lists, blockquote, links, and code blocks with per-block language +
syntax highlighting — the base set GitBook offered.

[`content.py`](content.py) is the server-side reference: `render_doc` (JSON→HTML for the
study view and list previews), `doc_to_text` (search / "has answer?"), and
`markdown_to_doc` (the GitBook importer). Cards have stable ids, so editing a card's
wording no longer orphans its FSRS history.

### GitBook is now import-only

The old GitBook/GitLab source survives as a one-time importer: `POST /api/import/gitbook`
fetches the export, converts each `<details>` block to a card (Markdown → ProseMirror),
and skips anything already imported.

## Setup

Needs **[uv](https://docs.astral.sh/uv/)** for dependencies (Python 3.11–3.12, pinned in
`.python-version`) and **PostgreSQL** for review history.

```bash
uv sync                       # creates .venv (Python 3.12) from uv.lock
createdb questions            # or point DATABASE_URL at any existing Postgres
cp .env.example .env          # set DATABASE_URL + a GitLab token (read_repository scope)
uv run uvicorn main:app --reload
```

Open http://127.0.0.1:8000. The `progress` / `reviews` / `fsrs_params` tables are created
on first run. To enable the Settings-tab optimizer (installs torch):

```bash
uv sync --extra optimizer
```

Keyboard: `1`–`4` grade (Again / Hard / Good / Easy), `N` / `→` skip, `Space` / `Enter`
reveal the answer.

## Spaced repetition (FSRS)

Grade each recall on FSRS's four ratings — **Again(1) / Hard(2) / Good(3) / Easy(4)**.
Each button shows the interval it would schedule (like Anki), computed with a fuzz-free
copy of the scheduler so the hint matches the button.

**Only a grade is recorded.** Skipping a card with `N` / `→` is treated as just looking —
it never counts as a repetition, so the schedule reflects real recall attempts. With
*FSRS-порядок* on, the picker serves overdue cards first (most overdue first), then
never-graded cards, then cards not yet due.

FSRS owns all scheduling — stability, difficulty, due date, and the retrievability
("удержание") shown in stats. Tune the target with `FSRS_RETENTION` (default 0.9).

The **Статистика** tab lists every graded card — question, average rating, last rating,
repetition count, current retention %, next-review date — sortable by any column and
filterable by theme, text, and "только к повторению".

### Optimising the weights

The 21 FSRS weights default to a population-average model. The **Настройки** tab shows how
many of your reviews *count toward training* (FSRS only learns from reviews spread across
different days) versus the 512 it needs, and once you're there an **Оптимизировать веса**
button trains weights on *your* review history, stores them in `fsrs_params`, and applies
them live — no restart. Below the threshold the run is refused rather than silently
persisting default weights over your current ones.

Training runs py-fsrs's `Optimizer`, which needs the extra (torch/numpy/pandas; Python
3.11/3.12 only — no torch wheels for 3.13, which is why `.python-version` pins 3.12):

```bash
uv sync --extra optimizer
```

Without it the tab still shows progress; the button explains what to install. The trained
weights live in the database, so they survive restarts and server moves.

### Storage

Review history is stored in **PostgreSQL** (required — set `DATABASE_URL`; the app
refuses to start without it):

```bash
DATABASE_URL=postgresql://user:pass@host:5432/questions
```

Persistence is **SQLModel** (SQLAlchemy 2.0) — models and engine in `gitbook/models.py`.
The schema is created automatically on first run — three tables:

- **`progress`** — one row per card. `card_json` (JSONB) holds the serialised `fsrs.Card`
  (the per-card FSRS state: `stability`, `difficulty`, `state`, `step`, `due`,
  `last_review`); `due`/`state` are denormalised for the picker, and `reps` /
  `rating_sum` / `last_rating` back the stats aggregates.
- **`reviews`** — the raw append-only log (`question_id`, `rating`, `reviewed_at`) the
  optimiser trains on.
- **`fsrs_params`** — trained weight sets. The newest row is loaded at startup, so
  optimised weights survive restarts.

Plus **`cards`** — your authored content (`question`/`answer` ProseMirror docs, theme,
subtheme, tags). FSRS `progress`/`reviews` are keyed by `cards.id`.

The live 21 FSRS **weights** are the in-memory `Scheduler` (built in `build_scheduler`):
newest `fsrs_params` row → else `FSRS_PARAMETERS` env → else the library default. Reads
use pooled sessions; each write takes a per-card Postgres advisory lock so the
read-modify-write around FSRS is atomic.

## How it works

```
content.py          card content core — ProseMirror render/text + CardRepository
gitbook/models.py   SQLModel tables (cards, progress, reviews, fsrs_params) + engine
gitbook/store.py    FSRS card persistence + review history + weight load/save/hot-swap
gitbook/optimizer.py FSRS weight training (Optimizer) — status + run
gitbook/parser.py,source.py,render.py   GitBook — now used only by the importer
main.py             FastAPI routes (study + card CRUD + import)
static/, templates/ study UI (Тренировка / Статистика / Настройки)
frontend/           React + Vite + TipTap card manager (served at /manage)
```

## API

| Route | Purpose |
| --- | --- |
| `GET /` | study app · `/manage` card manager (when built) |
| `GET /api/cards` | list card summaries (`?search=&theme=&subtheme=`) |
| `POST /api/cards` | create a card `{question, answer, theme, subtheme, tags}` |
| `GET /api/cards/{id}` | full card (ProseMirror docs, for the editor) |
| `PUT /api/cards/{id}` · `DELETE /api/cards/{id}` | update / delete |
| `GET /api/config` | rating legend + FSRS retention target |
| `GET /api/index` | theme/subtheme tree with counts |
| `POST /api/questions/random` | `{theme, subtheme, answered_only, mode, exclude[]}` → next card (due-first in `spaced` mode) with `progress` and per-rating `preview` intervals |
| `POST /api/reviews` | `{question_id, rating}` (1–4) → record a graded review |
| `GET /api/stats` | every graded card with avg/last rating, count, retrievability, next-review date |
| `GET /api/optimizer/status` · `POST /api/optimizer/run` | weight training status / run |
| `POST /api/import/gitbook` | one-time import of the GitBook export into cards |

## Tests

```bash
uv run python tests/test_content.py  # ProseMirror render/text + Markdown import
uv run python tests/test_parser.py   # GitBook parsing (used by the importer)
uv run python tests/test_store.py    # FSRS scheduling & persistence (PostgreSQL)
```

`test_parser` covers heading tracking, nested `<details>`, fenced code containing
`</details>`, and each GitBook directive against `tests/fixture.md`. `test_store` runs
FSRS with fuzzing disabled and checks per-rating previews are monotonic, intervals grow on
repeated Good, aggregates (avg/last/count), rating validation, and that FSRS card state
survives a reconnect. It targets `TEST_DATABASE_URL` (default a local `qt_test` database,
name must contain "test") and **skips cleanly** if no PostgreSQL server is reachable.
