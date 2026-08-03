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
    """Per-user FSRS card store with review history and trainable weights, on PostgreSQL.

    Every method is scoped to a ``user_id`` — users never see each other's schedule,
    history, or weights. Each user's schedulers (built from their own trained weights)
    are cached in memory and rebuilt lazily after they optimise.
    """

    def __init__(self, engine, settings) -> None:
        self.engine = engine
        self.settings = settings
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[Scheduler, Scheduler]] = {}

    # ------------------------------------------------------- scheduler access

    def _schedulers(self, user_id: str) -> tuple[Scheduler, Scheduler]:
        with self._lock:
            hit = self._cache.get(user_id)
        if hit is not None:
            return hit
        weights = load_saved_weights(self.engine, user_id)
        try:
            built = build_scheduler(self.settings, weights)
        except ValueError:
            # Saved weights failed FSRS's bounds check — fall back to defaults.
            built = build_scheduler(self.settings, None)
        with self._lock:
            self._cache[user_id] = built
        return built

    def invalidate(self, user_id: str) -> None:
        """Drop the cached schedulers so the next access rebuilds from saved weights."""
        with self._lock:
            self._cache.pop(user_id, None)

    def _preview_from(self, user_id: str, card_json: dict | None, now: datetime) -> dict[str, float]:
        """Seconds until the next review for each rating, from a given card state."""
        _, preview = self._schedulers(user_id)
        out: dict[str, float] = {}
        for value, name in RATING_NAMES.items():
            base = Card.from_dict(card_json) if card_json else Card(due=now)
            card, _ = preview.review_card(base, Rating(value), review_datetime=now)
            out[name] = max(0.0, card.due.timestamp() - now.timestamp())
        return out

    # ------------------------------------------------------------------ reads

    def schedules(self, user_id: str) -> dict[str, dict[str, float]]:
        """``{question_id: {due, state, reps}}`` for the picker."""
        with Session(self.engine) as session:
            rows = session.exec(
                select(Progress.question_id, Progress.due, Progress.state, Progress.reps)
                .where(Progress.user_id == user_id)
            ).all()
        return {r[0]: {"due": r[1], "state": r[2], "reps": r[3]} for r in rows}

    def stats(self, user_id: str, now: datetime | None = None) -> list[dict]:
        now = now or _now()
        scheduler, _ = self._schedulers(user_id)
        with Session(self.engine) as session:
            rows = session.exec(select(Progress).where(Progress.user_id == user_id)).all()
        return [_to_dict(p, scheduler, now) for p in rows]

    def get(self, user_id: str, question_id: str, now: datetime | None = None) -> dict | None:
        now = now or _now()
        scheduler, _ = self._schedulers(user_id)
        with Session(self.engine) as session:
            p = session.get(Progress, question_id)
        if p is None or p.user_id != user_id:
            return None
        return _to_dict(p, scheduler, now)

    def snapshot(self, user_id: str, question_id: str, now: datetime | None = None) -> dict:
        """Progress + per-rating preview from a SINGLE consistent card read."""
        now = now or _now()
        scheduler, _ = self._schedulers(user_id)
        with Session(self.engine) as session:
            p = session.get(Progress, question_id)
        if p is not None and p.user_id != user_id:
            p = None
        return {
            "progress": _to_dict(p, scheduler, now) if p else None,
            "preview": self._preview_from(user_id, p.card_json if p else None, now),
        }

    def dangling_history(self, user_id: str, live_ids: set[str]) -> dict[str, str]:
        """``{normalized question_text: question_id}`` for this user's progress rows whose
        card no longer exists — so a re-import can reuse the id and reconnect the history."""
        with Session(self.engine) as session:
            rows = session.exec(
                select(Progress.question_id, Progress.question_text)
                .where(Progress.user_id == user_id)
            ).all()
        out: dict[str, str] = {}
        for question_id, text in rows:
            if question_id in live_ids:
                continue
            key = (text or "").strip().lower()
            if key:
                out.setdefault(key, question_id)
        return out

    def reconnect_history(self, old_id: str, new_id: str) -> bool:
        """Move a dangling progress row (and its reviews) from ``old_id`` to ``new_id`` so
        old study history reattaches to an existing card. Merges aggregates if the target
        already has history, keeping the more recent FSRS state. No-op if nothing to move.
        """
        if old_id == new_id:
            return False
        with Session(self.engine) as session:
            old = session.get(Progress, old_id)
            if old is None:
                return False
            target = session.get(Progress, new_id)
            if target is None:
                session.add(Progress(
                    question_id=new_id, user_id=old.user_id, card_json=old.card_json,
                    due=old.due, state=old.state, reps=old.reps, rating_sum=old.rating_sum,
                    last_rating=old.last_rating, last_review=old.last_review,
                    theme=old.theme, subtheme=old.subtheme, section=old.section,
                    question_text=old.question_text,
                ))
            else:
                target.reps += old.reps
                target.rating_sum += old.rating_sum
                if old.last_review > target.last_review:  # keep the newer schedule state
                    target.last_review = old.last_review
                    target.last_rating = old.last_rating
                    target.card_json = old.card_json
                    target.due = old.due
                    target.state = old.state
                session.add(target)
            for review in session.exec(select(Review).where(Review.question_id == old_id)).all():
                review.question_id = new_id
                session.add(review)
            session.delete(old)
            session.commit()
            return True

    def purge_orphaned(self, user_id: str, live_ids: set[str]) -> dict[str, int]:
        """Delete this user's progress + review rows that aren't linked to a real card
        (question_id not among ``live_ids``). Cleans the 'removed from source' clutter."""
        deleted_progress = deleted_reviews = 0
        with Session(self.engine) as session:
            for p in session.exec(select(Progress).where(Progress.user_id == user_id)).all():
                if p.question_id not in live_ids:
                    session.delete(p); deleted_progress += 1
            for r in session.exec(select(Review).where(Review.user_id == user_id)).all():
                if r.question_id not in live_ids:
                    session.delete(r); deleted_reviews += 1
            session.commit()
        return {"progress": deleted_progress, "reviews": deleted_reviews}

    def review_count(self, user_id: str) -> int:
        with Session(self.engine) as session:
            return session.exec(
                select(func.count()).select_from(Review).where(Review.user_id == user_id)
            ).one()

    # ----------------------------------------------------------------- writes

    def record(
        self,
        user_id: str,
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
        scheduler, _ = self._schedulers(user_id)

        with Session(self.engine) as session:
            # Serialise the read-modify-write for this card across all connections.
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": question_id},
            )
            p = session.get(Progress, question_id)
            if p is not None and p.user_id != user_id:
                raise PermissionError("card belongs to another user")
            card = Card.from_dict(p.card_json) if p else Card(due=now)
            # ── the FSRS model runs here: updates DSR state and picks the next due ──
            card, _ = scheduler.review_card(card, Rating(rating), review_datetime=now)

            if p is None:
                p = Progress(question_id=question_id, user_id=user_id,
                             card_json=card.to_dict(), due=0, state=0)
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

            session.add(Review(user_id=user_id, question_id=question_id,
                               rating=rating, reviewed_at=now.timestamp()))
            result = _to_dict(p, scheduler, now)
            session.commit()
            return result

    # ------------------------------------------------------------ FSRS weights

    def latest_params(self, user_id: str) -> FsrsParams | None:
        with Session(self.engine) as session:
            return session.exec(
                select(FsrsParams).where(FsrsParams.user_id == user_id)
                .order_by(FsrsParams.id.desc()).limit(1)
            ).first()

    def save_weights(
        self, user_id: str, weights: list[float], desired_retention: float, review_count: int,
        now: datetime | None = None,
    ) -> FsrsParams:
        row = FsrsParams(
            user_id=user_id,
            weights=list(weights),
            desired_retention=desired_retention,
            review_count=review_count,
            trained_at=(now or _now()).timestamp(),
        )
        with Session(self.engine) as session:
            session.add(row)
            session.commit()
            session.refresh(row)
        self.invalidate(user_id)  # next access rebuilds schedulers from the new weights
        return row

    def review_logs(self, user_id: str) -> list:
        """Build ``fsrs.ReviewLog`` objects from this user's raw log, for the Optimizer."""
        from fsrs import ReviewLog

        with Session(self.engine) as session:
            rows = session.exec(
                select(Review.question_id, Review.rating, Review.reviewed_at)
                .where(Review.user_id == user_id)
                .order_by(Review.reviewed_at)
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


def load_saved_weights(engine, user_id: str) -> list[float] | None:
    with Session(engine) as session:
        row = session.exec(
            select(FsrsParams).where(FsrsParams.user_id == user_id)
            .order_by(FsrsParams.id.desc()).limit(1)
        ).first()
    return list(row.weights) if row else None


def open_store(settings) -> ReviewStore:
    """Wire up the store: engine + tables. Schedulers are built per user on demand."""
    if not settings.database_url:
        raise RuntimeError(
            "DATABASE_URL is required — this app stores review history in PostgreSQL. "
            "Set it in .env (see .env.example), e.g. "
            "postgresql://user:pass@host:5432/questions"
        )
    engine = make_engine(settings.database_url)
    return ReviewStore(engine, settings)
