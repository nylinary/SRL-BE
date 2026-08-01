"""Training the FSRS weights from your own review history.

The heavy lifting (gradient descent) is py-fsrs's ``Optimizer``, which needs the
``fsrs[optimizer]`` extra (torch/numpy/pandas/tqdm). When that isn't installed the
service still reports status; only :meth:`OptimizerService.run` requires it.

The library only learns from **non-same-day, Review-state** reviews, and silently
returns the default weights unless there are at least ``mini_batch_size`` (512) of
them. So the "ready" gate and the progress shown in the UI count those *effective*
reviews — not the raw total — and ``run`` refuses (and never persists) when a fit
would be a no-op, so a premature click can't overwrite good weights with defaults.

A successful run reads the review log, fits a new 21-weight set, persists it to
``fsrs_params`` (surviving restarts), and hot-swaps the live schedulers — no reload.
"""

from __future__ import annotations

from .store import ReviewStore, build_scheduler

# py-fsrs's mini_batch_size: below this many effective reviews the Optimizer returns
# the default weights unchanged. It is the real floor for training to do anything.
REQUIRED_EFFECTIVE_REVIEWS = 512


class OptimizerUnavailable(RuntimeError):
    """The fsrs[optimizer] extra (torch/pandas/tqdm) is not installed."""


class NotEnoughReviews(ValueError):
    """Too few effective reviews — a fit would be a no-op."""


def optimizer_available() -> bool:
    """True only when py-fsrs's *real* Optimizer is importable.

    fsrs falls back to a stub Optimizer (whose __init__ just raises) if any of
    torch/pandas/tqdm is missing; the stub lacks ``compute_optimal_parameters``.
    """
    try:
        from fsrs import Optimizer
    except ImportError:
        return False
    return hasattr(Optimizer, "compute_optimal_parameters")


def _effective_reviews(logs) -> int:
    """Count non-same-day, Review-state reviews — what the Optimizer actually learns from.

    Mirrors py-fsrs's internal ``_num_reviews`` (default scheduler, first 64 reviews per
    card, a review counts once its card has a prior review on an earlier day). Needs only
    the base fsrs package, so it works even without the optimizer extra.
    """
    from fsrs import Card, Scheduler

    scheduler = Scheduler()
    by_card: dict[int, list] = {}
    for log in logs:
        by_card.setdefault(log.card_id, []).append(log)

    total = 0
    for card_id, entries in by_card.items():
        entries = sorted(entries, key=lambda e: e.review_datetime)[:64]
        card = None
        for index, log in enumerate(entries):
            if index == 0:
                card = Card(card_id=card_id, due=log.review_datetime)
            if card.last_review and (log.review_datetime - card.last_review).days > 0:
                total += 1
            card, _ = scheduler.review_card(
                card, log.rating, review_datetime=log.review_datetime, review_duration=None
            )
    return total


def _is_noop(weights) -> bool:
    """True if the fit came back as the library's untouched default weights."""
    from fsrs.optimizer import DEFAULT_PARAMETERS

    return [round(float(w), 6) for w in weights] == [round(float(d), 6) for d in DEFAULT_PARAMETERS]


class OptimizerService:
    def __init__(self, store: ReviewStore, settings) -> None:
        self.store = store
        self.settings = settings

    def _current_weights_info(self) -> dict:
        params = self.store.latest_params()
        if params is not None:
            return {
                "source": "trained",
                "trained_at": params.trained_at,
                "trained_on_reviews": params.review_count,
                "desired_retention": params.desired_retention,
            }
        if self.settings.fsrs_parameters:
            return {"source": "custom", "trained_at": None}
        return {"source": "default", "trained_at": None}

    def status(self) -> dict:
        total = self.store.review_count()
        effective = _effective_reviews(self.store.review_logs())
        return {
            "review_count": total,
            "effective_reviews": effective,
            "required": REQUIRED_EFFECTIVE_REVIEWS,
            "remaining": max(0, REQUIRED_EFFECTIVE_REVIEWS - effective),
            "ready": effective >= REQUIRED_EFFECTIVE_REVIEWS,
            "optimizer_available": optimizer_available(),
            "retention": self.settings.fsrs_retention,
            "current": self._current_weights_info(),
        }

    def run(self) -> dict:
        """Optimise weights on the review log, persist, and apply live."""
        if not optimizer_available():
            raise OptimizerUnavailable(
                'The optimizer is not installed. Add it with: pip install "fsrs[optimizer]"'
            )

        logs = self.store.review_logs()
        effective = _effective_reviews(logs)
        if effective < REQUIRED_EFFECTIVE_REVIEWS:
            raise NotEnoughReviews(
                f"Need at least {REQUIRED_EFFECTIVE_REVIEWS} reviews spread across "
                f"different days to optimise (have {effective})."
            )

        from fsrs import Optimizer

        try:
            weights = Optimizer(tuple(logs)).compute_optimal_parameters()
        except ImportError as error:  # detection and construction must never diverge
            raise OptimizerUnavailable(str(error)) from error

        if _is_noop(weights):
            # The library declined to train; don't persist defaults over good weights.
            raise NotEnoughReviews(
                "Training produced no change — not enough spread-out reviews yet."
            )

        self.store.save_weights(weights, self.settings.fsrs_retention, self.store.review_count())
        live, preview = build_scheduler(self.settings, weights)
        self.store.set_schedulers(live, preview)   # applied with no restart

        result = self.status()
        result["trained"] = True
        result["weights"] = [round(float(w), 4) for w in weights]
        return result
