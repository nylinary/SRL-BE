"""Spaced-repetition storage backed by FSRS (py-fsrs) on PostgreSQL via SQLModel.

A *review* is recorded only when you grade yourself Again/Hard/Good/Easy (1–4).
Merely looking at a question records nothing — a deliberate requirement, so the
schedule reflects real recall attempts, not glances.

Each question maps to one FSRS :class:`fsrs.Card`, persisted as JSONB in the
``progress`` table. FSRS owns all scheduling (stability, difficulty, due date); this
module persists the card, keeps a review-history tally for the stats table, answers
"what is due now?", and stores/loads the trained model weights (``fsrs_params``).

Where FSRS state lives:
- the 21 model **weights** live in the in-memory :class:`fsrs.Scheduler`
  (see :func:`build_scheduler`); the newest trained set is mirrored to ``fsrs_params``
  so it survives restarts;
- each card's **DSR state** is the ``progress.card_json`` column;
- the raw **review log** is the ``reviews`` table.

Concurrency: reads use pooled sessions; each write takes a per-card Postgres advisory
lock so the read-modify-write around FSRS is atomic even across worker processes.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from fsrs import Card, Rating, Scheduler, State
from sqlalchemy import func, text
from sqlmodel import Session, select

from .models import FsrsParams, Progress, Review, make_engine

RATING_NAMES = {1: "again", 2: "hard", 3: "good", 4: "easy"}
STATE_NAMES = {
    int(State.Learning): "learning",
    int(State.Review): "review",
    int(State.Relearning): "relearning",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _avg(rating_sum: int, reps: int) -> float:
    return round(rating_sum / reps, 2) if reps else 0.0


def _to_dict(p: Progress, scheduler: Scheduler | None, now: datetime) -> dict:
    data = {
        "question_id": p.question_id,
        "theme": p.theme,
        "subtheme": p.subtheme,
        "section": p.section,
        "question_text": p.question_text,
        "count": p.reps,
        "avg_score": _avg(p.rating_sum, p.reps),
        "last_score": p.last_rating,
        "last_review": p.last_review,
        "next_due": p.due,
        "state": STATE_NAMES.get(p.state, "learning"),
    }
    if scheduler is not None:
        card = Card.from_dict(p.card_json)
        data["retrievability"] = round(
            scheduler.get_card_retrievability(card, current_datetime=now), 4
        )
    return data


class ReviewStore:
    """FSRS card store with review history and trainable weights, on PostgreSQL."""

    def __init__(
        self,
        engine,
        scheduler: Scheduler,
        preview_scheduler: Scheduler | None = None,
    ) -> None:
        self.engine = engine
        # Guards the in-memory scheduler swap so a hot reload can't tear a request.
        self._sched_lock = threading.Lock()
        self.scheduler = scheduler
        self.preview_scheduler = preview_scheduler or scheduler

    # ------------------------------------------------------- scheduler access

    def _schedulers(self) -> tuple[Scheduler, Scheduler]:
        with self._sched_lock:
            return self.scheduler, self.preview_scheduler

    def set_schedulers(self, scheduler: Scheduler, preview_scheduler: Scheduler) -> None:
        """Hot-swap the live schedulers (after optimising) with no restart."""
        with self._sched_lock:
            self.scheduler = scheduler
            self.preview_scheduler = preview_scheduler

    def _preview_from(self, card_json: dict | None, now: datetime) -> dict[str, float]:
        """Seconds until the next review for each rating, from a given card state.

        Uses the fuzz-free preview scheduler so the button hints match what they imply.
        """
        _, preview = self._schedulers()
        out: dict[str, float] = {}
        for value, name in RATING_NAMES.items():
            base = Card.from_dict(card_json) if card_json else Card(due=now)
            card, _ = preview.review_card(base, Rating(value), review_datetime=now)
            out[name] = max(0.0, card.due.timestamp() - now.timestamp())
        return out

    # ------------------------------------------------------------------ reads

    def schedules(self) -> dict[str, dict[str, float]]:
        """``{question_id: {due, state, reps}}`` for the picker."""
        with Session(self.engine) as session:
            rows = session.exec(
                select(Progress.question_id, Progress.due, Progress.state, Progress.reps)
            ).all()
        return {r[0]: {"due": r[1], "state": r[2], "reps": r[3]} for r in rows}

    def stats(self, now: datetime | None = None) -> list[dict]:
        now = now or _now()
        scheduler, _ = self._schedulers()
        with Session(self.engine) as session:
            rows = session.exec(select(Progress)).all()
        return [_to_dict(p, scheduler, now) for p in rows]

    def get(self, question_id: str, now: datetime | None = None) -> dict | None:
        now = now or _now()
        scheduler, _ = self._schedulers()
        with Session(self.engine) as session:
            p = session.get(Progress, question_id)
        return _to_dict(p, scheduler, now) if p else None

    def preview(self, question_id: str, now: datetime | None = None) -> dict[str, float]:
        now = now or _now()
        with Session(self.engine) as session:
            p = session.get(Progress, question_id)
        return self._preview_from(p.card_json if p else None, now)

    def snapshot(self, question_id: str, now: datetime | None = None) -> dict:
        """Progress + per-rating preview from a SINGLE consistent card read."""
        now = now or _now()
        scheduler, _ = self._schedulers()
        with Session(self.engine) as session:
            p = session.get(Progress, question_id)
        return {
            "progress": _to_dict(p, scheduler, now) if p else None,
            "preview": self._preview_from(p.card_json if p else None, now),
        }

    def review_count(self) -> int:
        with Session(self.engine) as session:
            return session.exec(select(func.count()).select_from(Review)).one()

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
        scheduler, _ = self._schedulers()

        with Session(self.engine) as session:
            # Serialise the read-modify-write for this card across all connections.
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": question_id},
            )
            p = session.get(Progress, question_id)
            card = Card.from_dict(p.card_json) if p else Card(due=now)
            # ── the FSRS model runs here: updates DSR state and picks the next due ──
            card, _ = scheduler.review_card(card, Rating(rating), review_datetime=now)

            if p is None:
                p = Progress(question_id=question_id, card_json=card.to_dict(), due=0, state=0)
                session.add(p)

            p.card_json = card.to_dict()
            p.due = card.due.timestamp()
            p.state = int(card.state)
            p.reps += 1
            p.rating_sum += rating
            p.last_rating = rating
            p.last_review = now.timestamp()
            p.theme = meta.get("theme", p.theme)
            p.subtheme = meta.get("subtheme", p.subtheme)
            p.section = meta.get("section", p.section)
            p.question_text = meta.get("question_text", p.question_text)

            session.add(Review(question_id=question_id, rating=rating, reviewed_at=now.timestamp()))
            result = _to_dict(p, scheduler, now)
            session.commit()
            return result

    # ------------------------------------------------------------ FSRS weights

    def latest_params(self) -> FsrsParams | None:
        with Session(self.engine) as session:
            return session.exec(
                select(FsrsParams).order_by(FsrsParams.id.desc()).limit(1)
            ).first()

    def save_weights(
        self, weights: list[float], desired_retention: float, review_count: int,
        now: datetime | None = None,
    ) -> FsrsParams:
        row = FsrsParams(
            weights=list(weights),
            desired_retention=desired_retention,
            review_count=review_count,
            trained_at=(now or _now()).timestamp(),
        )
        with Session(self.engine) as session:
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def review_logs(self) -> list:
        """Build ``fsrs.ReviewLog`` objects from the raw log, for the Optimizer.

        Card identity only needs to be consistent per question, so each question maps
        to a synthetic integer card id; the optimizer groups and time-sorts internally.
        """
        from fsrs import ReviewLog

        with Session(self.engine) as session:
            rows = session.exec(
                select(Review.question_id, Review.rating, Review.reviewed_at).order_by(
                    Review.reviewed_at
                )
            ).all()
        card_ids: dict[str, int] = {}
        logs = []
        for question_id, rating, reviewed_at in rows:
            card_id = card_ids.setdefault(question_id, len(card_ids) + 1)
            logs.append(
                ReviewLog(
                    card_id=card_id,
                    rating=Rating(rating),
                    review_datetime=datetime.fromtimestamp(reviewed_at, tz=timezone.utc),
                    review_duration=None,
                )
            )
        return logs

    def close(self) -> None:
        self.engine.dispose()


# ------------------------------------------------------------------- factories


def build_scheduler(settings, weights: list[float] | None = None) -> tuple[Scheduler, Scheduler]:
    """Return (live scheduler, fuzz-free preview scheduler).

    ``weights`` wins if given (e.g. loaded from ``fsrs_params`` or freshly optimised),
    else ``FSRS_PARAMETERS`` from the environment, else the library defaults.
    """
    kwargs = dict(
        desired_retention=settings.fsrs_retention,
        maximum_interval=settings.fsrs_max_interval,
    )
    params = weights or settings.fsrs_parameters
    if params:
        kwargs["parameters"] = tuple(params)
    live = Scheduler(enable_fuzzing=settings.fsrs_enable_fuzz, **kwargs)
    preview = Scheduler(enable_fuzzing=False, **kwargs)
    return live, preview


def load_saved_weights(engine) -> list[float] | None:
    with Session(engine) as session:
        row = session.exec(select(FsrsParams).order_by(FsrsParams.id.desc()).limit(1)).first()
    return list(row.weights) if row else None


def open_store(settings) -> ReviewStore:
    """Wire up the store: engine, tables, and schedulers seeded from saved weights."""
    if not settings.database_url:
        raise RuntimeError(
            "DATABASE_URL is required — this app stores review history in PostgreSQL. "
            "Set it in .env (see .env.example), e.g. "
            "postgresql://user:pass@host:5432/questions"
        )
    engine = make_engine(settings.database_url)
    weights = load_saved_weights(engine)
    try:
        live, preview = build_scheduler(settings, weights)
    except ValueError:
        # Saved weights failed FSRS's bounds check — fall back to defaults rather
        # than refuse to boot.
        live, preview = build_scheduler(settings, None)
    return ReviewStore(engine, live, preview)
