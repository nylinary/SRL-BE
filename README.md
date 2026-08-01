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
("удержание") shown in stats. Tune it with `FSRS_RETENTION` (default 0.9). Once you have
review history you can train optimised weights and paste them into `FSRS_PARAMETERS`.

The **Статистика** tab lists every graded card — question, average rating, last rating,
repetition count, current retention %, next-review date — sortable by any column and
filterable by theme, text, and "только к повторению".

### Storage

Review history is stored in **PostgreSQL** (required — set `DATABASE_URL`; the app
refuses to start without it):

```bash
DATABASE_URL=postgresql://user:pass@host:5432/questions
```

The schema is created automatically on first run — two tables:

- **`progress`** — one row per card. `card_json` holds the serialised `fsrs.Card`
  (the per-card FSRS state: `stability`, `difficulty`, `state`, `step`, `due`,
  `last_review`); `due`/`state` are denormalised for the picker, and `reps` /
  `rating_sum` / `last_rating` back the stats aggregates.
- **`reviews`** — the raw append-only log (`question_id`, `rating`, `reviewed_at`),
  the data an optimiser would train new weights on.

The 21 FSRS **weights** are *not* in the database — they're the in-memory model built
in `build_scheduler` from `FSRS_PARAMETERS` (or the library default), the same for
every card. Cards are keyed by a content hash, so editing a question's wording in
GitBook starts a fresh card (the old one lingers in stats, flagged `удалён из источника`).

## How it works

```
gitbook/config.py   settings from env / .env (incl. FSRS knobs)
gitbook/source.py   GitLab fetch, TTL cache, on-disk fallback copy
gitbook/parser.py   markdown -> [Question(theme, subtheme, question, body)]
gitbook/render.py   GitBook-flavoured markdown -> HTML
gitbook/store.py    FSRS Card persistence (PostgreSQL) + review history
main.py             FastAPI routes
static/, templates/ single-page UI (Тренировка / Статистика tabs)
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
