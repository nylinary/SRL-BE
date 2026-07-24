# Question Trainer

Flashcard-style drilling over a GitBook export (`questions.md`) kept in a GitLab repo.
Shows one random question at a time with the answer collapsed, so you can recall first
and check afterwards.

## Document contract

The parser mirrors how the GitBook page is authored:

| Markdown | Meaning |
| --- | --- |
| `# Заголовок` | **Theme** — БД, Брокеры сообщений, … |
| `## Заголовок` | **Subtheme** — PostgreSQL, Apache Kafka, … |
| `### Заголовок` | optional extra section |
| `<details><summary>…</summary>` | **Question** |
| body of the `<details>` | **Answer** (any markdown/HTML, nested `<details>` included) |

Questions without a body are kept and marked "ответ ещё не записан" — the
*Только с ответом* switch decides whether they show up.

## Setup

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # then paste a GitLab token with read_repository scope
.venv/bin/uvicorn main:app --reload
```

Open http://127.0.0.1:8000.

Keyboard: `N` / `→` next question, `Space` / `Enter` reveal the answer.

## How it works

```
gitbook/config.py   settings from env / .env
gitbook/source.py   GitLab fetch, TTL cache, on-disk fallback copy
gitbook/parser.py   markdown -> [Question(theme, subtheme, question, body)]
gitbook/render.py   GitBook-flavoured markdown -> HTML
main.py             FastAPI routes
static/, templates/ single-page UI (no iframe)
```

The last successful download is mirrored to `.cache/questions.md`, so an expired
token or a GitLab outage degrades to an "офлайн-копия" badge instead of a blank page.

GitBook-specific syntax is expanded rather than leaked as literal text:
`{% hint %}` → callouts, `{% code title %}` → captioned code blocks,
`{% tabs %}`, `{% stepper %}`, `{% columns %}`, `{% embed %}`, and
`<mark style="color:$primary">` → themed highlights. Repository images
(`../.gitbook/assets/…`) are served through the authenticated `/asset` proxy.

## API

| Route | Purpose |
| --- | --- |
| `GET /` | the app |
| `GET /api/index` | theme/subtheme tree with counts + source status |
| `POST /api/questions/random` | `{theme, subtheme, answered_only, seen[]}` → a question not in `seen` |
| `POST /api/refresh` | force a re-download |
| `GET /asset?path=…` | authenticated image proxy |

## Tests

```bash
.venv/bin/python tests/test_parser.py
```

Covers heading tracking, nested `<details>`, fenced code containing `</details>`,
and each GitBook directive, against `tests/fixture.md`.
