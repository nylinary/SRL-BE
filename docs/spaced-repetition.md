# Spaced Repetition (FSRS)

Scheduling is handled by **py-fsrs** (FSRS-6). A *review* is recorded **only** when the
user grades themselves Again/Hard/Good/Easy (1–4) — merely viewing a card records nothing,
so the schedule reflects real recall attempts.

## How it works

- Each card maps to one `fsrs.Card`, persisted as `progress.card_json` (the DSR state:
  difficulty, stability, due date).
- Grading calls `scheduler.review_card(...)`, which updates that state and picks the next
  due time; the review is appended to `reviews`.
- The **preview** intervals under each grade button come from a fuzz-free scheduler so the
  "+10 min / +8 d" hints match what actually happens.

## Per-user weights

FSRS has 21 model **weights**. Each user has their own:

- Built from that user's newest `fsrs_params` row, else `FSRS_PARAMETERS` from the env,
  else the library defaults.
- Cached in-process per user (`ReviewStore._schedulers`) and rebuilt lazily after training
  — no restart needed (`save_weights` invalidates the cache).

## The optimizer (Settings tab → "Optimize")

`OptimizerService` (per user):

- `status(user_id)` reports the review count, the count of **effective** reviews (non-same-
  day, Review-state — the only ones training learns from), the readiness gate
  (`REQUIRED_EFFECTIVE_REVIEWS = 512`), and the current weight source.
- `run(user_id)` fits new weights from the user's review log, refuses if a fit would be a
  no-op (so a premature click can't overwrite good weights with defaults), persists them,
  and applies them live.

The optimizer needs the `fsrs[optimizer]` extra (torch/numpy/pandas/tqdm). It's installed
in production via `uv sync --extra optimizer`. Without it, `status` still works but `run`
returns `501`. On Linux, torch is pinned to the CPU-only wheel (see `pyproject.toml`).

## Relevant env

`FSRS_RETENTION` (target recall probability, 0.8–0.97) · `FSRS_MAX_INTERVAL` ·
`FSRS_ENABLE_FUZZ` · `FSRS_PARAMETERS` (optional starting weights; a trained per-user set
takes precedence). See [deployment.md](deployment.md).
