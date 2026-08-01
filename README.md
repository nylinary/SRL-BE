# Question Trainer

Flashcard-style drilling over a GitBook export (`questions.md`) kept in a GitLab repo,
scheduled with **[FSRS](https://github.com/open-spaced-repetition/py-fsrs)**. Shows one
question at a time with the answer collapsed, so you recall first and check afterwards;
grading each recall feeds FSRS, which decides when you see the card again.

## Document contract

The parser mirrors how the GitBook page is authored:

| Markdown | Meaning |
| --- | --- |
| `# Заголовок` / `## Заголовок` | **Theme** — the outermost heading that actually varies (a single wrapping title is skipped) |
| next heading level | **Subtheme** — PostgreSQL, Apache Kafka, … |
| following level | optional extra **section** |
| `<details><summary>…</summary>` | **Question** |
| body of the `<details>` | **Answer** (any markdown/HTML, nested `<details>` included) |

Questions without a body are kept and marked "ответ ещё не записан" — the *Только с
ответом* switch decides whether they show up.

## Setup

Requires **PostgreSQL** for review history.

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
createdb questions            # or point DATABASE_URL at any existing Postgres
cp .env.example .env          # set DATABASE_URL + a GitLab token (read_repository scope)
.venv/bin/uvicorn main:app --reload
```

Open http://127.0.0.1:8000. The `progress` / `reviews` tables are created on first run.

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

Training runs py-fsrs's `Optimizer`, which needs the extra:

```bash
pip install "fsrs[optimizer]"   # torch/numpy/pandas; use Python 3.11/3.12 — no torch wheels for 3.13
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

The live 21 FSRS **weights** are the in-memory `Scheduler` (built in `build_scheduler`):
newest `fsrs_params` row → else `FSRS_PARAMETERS` env → else the library default. Reads
use pooled sessions; each write takes a per-card Postgres advisory lock so the
read-modify-write around FSRS is atomic. Cards are keyed by a content hash, so editing a
question's wording in GitBook starts a fresh card (the old one lingers in stats, flagged
`удалён из источника`).

## How it works

```
gitbook/config.py   settings from env / .env (incl. FSRS knobs)
gitbook/source.py   GitLab fetch, TTL cache, on-disk fallback copy
gitbook/parser.py   markdown -> [Question(theme, subtheme, question, body)]
gitbook/render.py   GitBook-flavoured markdown -> HTML
gitbook/models.py   SQLModel tables (progress, reviews, fsrs_params) + engine
gitbook/store.py    FSRS Card persistence + review history + weight load/save/hot-swap
gitbook/optimizer.py FSRS weight training (Optimizer) — status + run
main.py             FastAPI routes
static/, templates/ single-page UI (Тренировка / Статистика / Настройки tabs)
```

The last successful download is mirrored to `.cache/questions.md`, so an expired token or
a GitLab outage degrades to an "офлайн-копия" badge instead of a blank page. GitBook
syntax is expanded rather than leaked as literal text: `{% hint %}` → callouts,
`{% code title %}` → captioned code blocks, `{% tabs %}` / `{% stepper %}` / `{% columns %}`,
`<mark style="color:$primary">` → themed highlights, `:circle-N:` → ①②③. Repository images
(`../.gitbook/assets/…`) are served through the authenticated `/asset` proxy.

## API

| Route | Purpose |
| --- | --- |
| `GET /` | the app |
| `GET /api/config` | rating legend + FSRS retention target |
| `GET /api/index` | theme/subtheme tree with counts + source status |
| `POST /api/questions/random` | `{theme, subtheme, answered_only, mode, seen[]}` → next card (due-first in `spaced` mode) with its `progress` and per-rating `preview` intervals |
| `POST /api/reviews` | `{question_id, rating}` (1–4) → record a graded review, advance the FSRS schedule |
| `GET /api/stats` | every graded card with avg/last rating, count, retrievability, next-review date |
| `GET /api/optimizer/status` | review count vs. thresholds + current weight source |
| `POST /api/optimizer/run` | train weights on the review log, persist to `fsrs_params`, apply live (501 if the extra isn't installed, 400 if too few reviews) |
| `POST /api/refresh` | force a re-download |
| `GET /asset?path=…` | authenticated image proxy |

## Tests

```bash
.venv/bin/python tests/test_parser.py   # parsing & GitBook rendering
.venv/bin/python tests/test_store.py    # FSRS scheduling & persistence (PostgreSQL)
```

`test_parser` covers heading tracking, nested `<details>`, fenced code containing
`</details>`, and each GitBook directive against `tests/fixture.md`. `test_store` runs
FSRS with fuzzing disabled and checks per-rating previews are monotonic, intervals grow on
repeated Good, aggregates (avg/last/count), rating validation, and that FSRS card state
survives a reconnect. It targets `TEST_DATABASE_URL` (default a local `qt_test` database,
name must contain "test") and **skips cleanly** if no PostgreSQL server is reachable.
