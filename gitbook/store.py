"""Spaced-repetition storage backed by the FSRS algorithm (py-fsrs) on PostgreSQL.

A *review* is recorded only when you grade yourself Again/Hard/Good/Easy (1–4).
Merely looking at a question records nothing — a deliberate requirement, so the
schedule reflects real recall attempts, not glances.

Each question maps to one FSRS :class:`fsrs.Card`, persisted as JSON in the
``progress`` table. FSRS owns all scheduling (stability, difficulty, due date); this
module only persists the card, keeps a review-history tally for the stats table, and
answers "what is due now?".

Where FSRS state lives:
- the 21 model **weights** are NOT stored here — they live in the in-memory
  ``Scheduler`` (see :func:`build_scheduler`), the same for every card;
- each card's **DSR state** (stability/difficulty/state/step/due) is the
  ``progress.card_json`` column, a serialised ``fsrs.Card``;
- the raw **review log** (rating + timestamp) is the ``reviews`` table.

One connection is shared across FastAPI's threadpool and serialised by a lock —
plenty for a single-user trainer. Cards are keyed by the content hash
:attr:`gitbook.parser.Question.id`, so a card keeps its history while the question
text is unchanged.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from fsrs import Card, Rating, Scheduler, State

RATING_NAMES = {1: "again", 2: "hard", 3: "good", 4: "easy"}
STATE_NAMES = {
    int(State.Learning): "learning",
    int(State.Review): "review",
    int(State.Relearning): "relearning",
}

# Column order shared by the SELECT, the row unpacking and the upsert params.
PROGRESS_COLS = [
    "question_id", "card_json", "due", "state", "reps", "rating_sum",
    "last_rating", "last_review", "theme", "subtheme", "section", "question_text",
]
_COLUMNS = ", ".join(PROGRESS_COLS)
_PLACEHOLDERS = ", ".join(["%s"] * len(PROGRESS_COLS))
_UPDATES = ", ".join(f"{c} = EXCLUDED.{c}" for c in PROGRESS_COLS if c != "question_id")

_UPSERT_SQL = (
    f"INSERT INTO progress ({_COLUMNS}) VALUES ({_PLACEHOLDERS}) "
    f"ON CONFLICT (question_id) DO UPDATE SET {_UPDATES}"
)
_INSERT_REVIEW = (
    "INSERT INTO reviews (question_id, rating, reviewed_at) VALUES (%s, %s, %s)"
)

_DDL = [
    """
    CREATE TABLE IF NOT EXISTS progress (
        question_id   TEXT PRIMARY KEY,
        card_json     TEXT NOT NULL,
        due           DOUBLE PRECISION NOT NULL,
        state         INTEGER NOT NULL,
        reps          INTEGER NOT NULL DEFAULT 0,
        rating_sum    INTEGER NOT NULL DEFAULT 0,
        last_rating   INTEGER NOT NULL DEFAULT 0,
        last_review   DOUBLE PRECISION NOT NULL DEFAULT 0,
        theme         TEXT NOT NULL DEFAULT '',
        subtheme      TEXT NOT NULL DEFAULT '',
        section       TEXT NOT NULL DEFAULT '',
        question_text TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reviews (
        id          BIGSERIAL PRIMARY KEY,
        question_id TEXT NOT NULL,
        rating      INTEGER NOT NULL,
        reviewed_at DOUBLE PRECISION NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_due ON progress(due)",
    "CREATE INDEX IF NOT EXISTS idx_reviews_qid ON reviews(question_id)",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Progress:
    question_id: str
    card: dict
    due: float
    state: int
    reps: int
    rating_sum: int
    last_rating: int
    last_review: float
    theme: str = ""
    subtheme: str = ""
    section: str = ""
    question_text: str = ""

    @property
    def avg_rating(self) -> float:
        return round(self.rating_sum / self.reps, 2) if self.reps else 0.0

    def as_dict(self, scheduler: Scheduler | None = None, now: datetime | None = None) -> dict:
        data = {
            "question_id": self.question_id,
            "theme": self.theme,
            "subtheme": self.subtheme,
            "section": self.section,
            "question_text": self.question_text,
            "count": self.reps,
            "avg_score": self.avg_rating,
            "last_score": self.last_rating,
            "last_review": self.last_review,
            "next_due": self.due,
            "state": STATE_NAMES.get(self.state, "learning"),
        }
        if scheduler is not None:
            card = Card.from_dict(self.card)
            data["retrievability"] = round(
                scheduler.get_card_retrievability(card, current_datetime=now or _now()), 4
            )
        return data


def _row_to_progress(row) -> Progress:
    (
        question_id, card_json, due, state, reps, rating_sum,
        last_rating, last_review, theme, subtheme, section, question_text,
    ) = row
    return Progress(
        question_id=question_id,
        card=json.loads(card_json),
        due=due,
        state=state,
        reps=reps,
        rating_sum=rating_sum,
        last_rating=last_rating,
        last_review=last_review,
        theme=theme,
        subtheme=subtheme,
        section=section,
        question_text=question_text,
    )


class ReviewStore:
    """Thread-safe FSRS card store with review history, on PostgreSQL."""

    def __init__(
        self,
        dsn: str,
        scheduler: Scheduler,
        preview_scheduler: Scheduler | None = None,
    ) -> None:
        import psycopg  # imported lazily so the dependency is only needed at runtime

        self.scheduler = scheduler
        # Previews (the "next interval per rating" hints) must be stable across
        # calls, so they use a fuzz-free clone of the scheduler.
        self.preview_scheduler = preview_scheduler or scheduler
        self._lock = threading.Lock()
        self._conn = psycopg.connect(dsn, autocommit=True)
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            for statement in _DDL:
                self._conn.execute(statement)

    # ------------------------------------------------------------------ reads

    def _get(self, question_id: str) -> Progress | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM progress WHERE question_id = %s", (question_id,)
        ).fetchone()
        return _row_to_progress(row) if row else None

    def schedules(self) -> dict[str, dict[str, float]]:
        """``{question_id: {due, state, reps}}`` for the picker."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT question_id, due, state, reps FROM progress"
            ).fetchall()
        return {row[0]: {"due": row[1], "state": row[2], "reps": row[3]} for row in rows}

    def stats(self, now: datetime | None = None) -> list[dict]:
        now = now or _now()
        with self._lock:
            rows = self._conn.execute(f"SELECT {_COLUMNS} FROM progress").fetchall()
        return [_row_to_progress(row).as_dict(self.scheduler, now) for row in rows]

    def get(self, question_id: str, now: datetime | None = None) -> dict | None:
        with self._lock:
            progress = self._get(question_id)
        return progress.as_dict(self.scheduler, now or _now()) if progress else None

    def _preview_from(self, stored: Progress | None, now: datetime) -> dict[str, float]:
        """Seconds until the next review for each rating, from a given card state.

        Mirrors Anki's per-button interval hints. Uses the fuzz-free scheduler so
        the numbers shown match what the buttons imply (the real review may be
        fuzzed by a few percent).
        """
        out: dict[str, float] = {}
        for value, name in RATING_NAMES.items():
            base = Card.from_dict(stored.card) if stored else Card(due=now)
            card, _ = self.preview_scheduler.review_card(
                base, Rating(value), review_datetime=now
            )
            out[name] = max(0.0, card.due.timestamp() - now.timestamp())
        return out

    def preview(self, question_id: str, now: datetime | None = None) -> dict[str, float]:
        now = now or _now()
        with self._lock:
            stored = self._get(question_id)
        return self._preview_from(stored, now)

    def snapshot(self, question_id: str, now: datetime | None = None) -> dict:
        """Progress + per-rating preview from a SINGLE consistent card read.

        The card is read once under one lock, so ``progress`` and ``preview`` always
        describe the same card state even if a concurrent grade lands mid-request.
        """
        now = now or _now()
        with self._lock:
            stored = self._get(question_id)
        return {
            "progress": stored.as_dict(self.scheduler, now) if stored else None,
            "preview": self._preview_from(stored, now),
        }

    # ----------------------------------------------------------------- writes

    def record(
        self,
        question_id: str,
        rating: int,
        *,
        meta: dict[str, str] | None = None,
        now: datetime | None = None,
    ) -> dict:
        """Grade a card (1–4), advance its FSRS schedule, and return the new state."""
        if rating not in RATING_NAMES:
            raise ValueError("rating must be 1 (Again), 2 (Hard), 3 (Good) or 4 (Easy)")
        now = now or _now()
        meta = meta or {}

        with self._lock, self._conn.transaction():
            prev = self._get(question_id)
            card = Card.from_dict(prev.card) if prev else Card(due=now)
            # ── the FSRS model runs here: updates DSR state and picks the next due ──
            card, _ = self.scheduler.review_card(card, Rating(rating), review_datetime=now)

            progress = Progress(
                question_id=question_id,
                card=card.to_dict(),
                due=card.due.timestamp(),
                state=int(card.state),
                reps=(prev.reps if prev else 0) + 1,
                rating_sum=(prev.rating_sum if prev else 0) + rating,
                last_rating=rating,
                last_review=now.timestamp(),
                theme=meta.get("theme", prev.theme if prev else ""),
                subtheme=meta.get("subtheme", prev.subtheme if prev else ""),
                section=meta.get("section", prev.section if prev else ""),
                question_text=meta.get("question_text", prev.question_text if prev else ""),
            )
            self._conn.execute(
                _UPSERT_SQL,
                (
                    progress.question_id,
                    json.dumps(progress.card),
                    progress.due,
                    progress.state,
                    progress.reps,
                    progress.rating_sum,
                    progress.last_rating,
                    progress.last_review,
                    progress.theme,
                    progress.subtheme,
                    progress.section,
                    progress.question_text,
                ),
            )
            self._conn.execute(_INSERT_REVIEW, (question_id, rating, now.timestamp()))
            return progress.as_dict(self.scheduler, now)

    def close(self) -> None:
        self._conn.close()


# ------------------------------------------------------------------- factories


def build_scheduler(settings) -> tuple[Scheduler, Scheduler]:
    """Return (live scheduler, fuzz-free preview scheduler) from settings.

    The 21 FSRS weights live here, in memory — either the library defaults or the
    ``FSRS_PARAMETERS`` override. They are global (not per-card) and are never
    written to the database.
    """
    kwargs = dict(
        desired_retention=settings.fsrs_retention,
        maximum_interval=settings.fsrs_max_interval,
    )
    if settings.fsrs_parameters:
        kwargs["parameters"] = settings.fsrs_parameters
    live = Scheduler(enable_fuzzing=settings.fsrs_enable_fuzz, **kwargs)
    preview = Scheduler(enable_fuzzing=False, **kwargs)
    return live, preview


def make_store(settings, scheduler: Scheduler, preview_scheduler: Scheduler) -> ReviewStore:
    if not settings.database_url:
        raise RuntimeError(
            "DATABASE_URL is required — this app stores review history in PostgreSQL. "
            "Set it in .env (see .env.example), e.g. "
            "postgresql://user:pass@host:5432/questions"
        )
    return ReviewStore(settings.database_url, scheduler, preview_scheduler)
