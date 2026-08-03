"""Checks for the per-user FSRS store on PostgreSQL: `python tests/test_store.py`.

Targets ``TEST_DATABASE_URL`` (default a local ``qt_test`` database). The database
name must contain "test" — a guard so the destructive table reset can never hit a
real database. If no PostgreSQL server is reachable, the suite SKIPS (exit 0)
rather than failing. Fuzzing is disabled so intervals are deterministic.
"""

from __future__ import annotations

import getpass
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402
from fsrs import Scheduler  # noqa: E402

from gitbook.models import make_engine  # noqa: E402
from gitbook.store import ReviewStore, load_saved_weights  # noqa: E402

URL = os.environ.get(
    "TEST_DATABASE_URL", f"postgresql://{getpass.getuser()}@127.0.0.1:5432/qt_test"
)
DBNAME = URL.rsplit("/", 1)[-1].split("?")[0]

if "test" not in DBNAME.lower():
    print(f"SKIP: database '{DBNAME}' is not a test database (name must contain 'test').")
    sys.exit(0)


def ensure_database() -> bool:
    try:
        psycopg.connect(URL).close()
        return True
    except psycopg.OperationalError:
        pass  # database may simply not exist yet — try to create it
    try:
        with psycopg.connect(URL.rsplit("/", 1)[0] + "/postgres", autocommit=True) as admin:
            admin.execute(f'CREATE DATABASE "{DBNAME}"')
        return True
    except Exception as error:  # server down / no permission → skip, don't fail
        print(f"SKIP: PostgreSQL unavailable ({type(error).__name__}: {str(error)[:80]})")
        return False


if not ensure_database():
    sys.exit(0)

# Clean slate — the store recreates the tables on construction.
with psycopg.connect(URL, autocommit=True) as conn:
    conn.execute("DROP TABLE IF EXISTS progress")
    conn.execute("DROP TABLE IF EXISTS reviews")
    conn.execute("DROP TABLE IF EXISTS fsrs_params")

# Two users prove isolation; everything is scoped to USER unless noted.
USER = "user-test"
OTHER = "user-other"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

SETTINGS = type("S", (), {
    "fsrs_retention": 0.9, "fsrs_max_interval": 36500,
    "fsrs_enable_fuzz": False, "fsrs_parameters": None,
})()

failures: list[str] = []
recorded: set[str] = set()


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label} {detail}".strip())
        print(f"  FAIL {label} {detail}")


def new_store() -> ReviewStore:
    return ReviewStore(make_engine(URL), SETTINGS)


def grade(store: ReviewStore, qid: str, rating: int, when: datetime, **meta) -> dict:
    recorded.add(qid)
    return store.record(USER, qid, rating, meta=meta or None, now=when)


def preview_of(store: ReviewStore, qid: str, when: datetime) -> dict:
    return store.snapshot(USER, qid, when)["preview"]


store = new_store()

print(f"target: {DBNAME}")
print("empty state")
check("unknown question has no progress", store.get(USER, "qX") is None)
check("schedules start empty", store.schedules(USER) == {})

print("preview (per-rating next interval)")
preview = preview_of(store, "qX", NOW)
check("preview covers all four ratings", set(preview) == {"again", "hard", "good", "easy"})
check(
    "preview is monotonic (Again ≤ Hard ≤ Good ≤ Easy)",
    preview["again"] <= preview["hard"] <= preview["good"] <= preview["easy"],
    str(preview),
)
check("Easy on a new card schedules days out", preview["easy"] > 86400, str(preview["easy"]))

print("recording a review")
p1 = grade(store, "qX", 3, NOW, theme="БД", subtheme="PostgreSQL", question_text="Что такое MVCC?")
check("recording creates progress", store.get(USER, "qX", NOW) is not None)
check("count is 1 after one review", p1["count"] == 1, str(p1["count"]))
check("avg equals the only rating", p1["avg_score"] == 3, str(p1["avg_score"]))
check("last_score is the rating", p1["last_score"] == 3, str(p1["last_score"]))
check("next_due is in the future", p1["next_due"] > NOW.timestamp())
check("meta stored for the stats table", p1["question_text"] == "Что такое MVCC?")
check("retrievability is a probability", 0.0 <= p1["retrievability"] <= 1.0, str(p1["retrievability"]))
check("fresh card is in a learning state", p1["state"] in {"learning", "review"}, p1["state"])

print("per-user isolation")
check("another user can't see the card", store.get(OTHER, "qX", NOW) is None)
check("another user's schedules stay empty", store.schedules(OTHER) == {})
try:
    store.record(OTHER, "qX", 3, now=NOW)
    check("cross-user record is blocked", False)
except PermissionError:
    check("cross-user record is blocked", True)

print("FSRS dynamics")
again = grade(store, "dynAgain", 1, NOW)
good = grade(store, "dynGood", 3, NOW)
check("Again schedules sooner than Good", again["next_due"] < good["next_due"],
      f"{again['next_due']} vs {good['next_due']}")

t = NOW
intervals = []
for _ in range(4):
    p = grade(store, "dynChain", 3, t)
    intervals.append(p["next_due"] - t.timestamp())
    t = datetime.fromtimestamp(p["next_due"], tz=timezone.utc)
check("repeated Good lengthens the interval",
      intervals == sorted(intervals) and intervals[-1] > intervals[0],
      str([round(i / 86400, 2) for i in intervals]))

print("aggregates & history")
p2 = grade(store, "qX", 4, NOW + timedelta(days=1))
check("count increments", p2["count"] == 2, str(p2["count"]))
check("avg is the running mean", p2["avg_score"] == 3.5, str(p2["avg_score"]))
check("last_score follows the latest grade", p2["last_score"] == 4, str(p2["last_score"]))
check("meta persists without re-sending", p2["theme"] == "БД", p2["theme"])

grade(store, "qY", 2, NOW, question_text="Redis?")
ids = {row["question_id"] for row in store.stats(USER, NOW)}
check("stats list exactly the graded cards", ids == recorded, f"{ids} vs {recorded}")

sched_map = store.schedules(USER)
check("schedules expose due epochs", "qX" in sched_map and sched_map["qX"]["due"] > 0)

print("weights & optimizer inputs")
valid_weights = list(Scheduler().parameters)
store.save_weights(USER, valid_weights, 0.9, store.review_count(USER))
check("weights persist and load back",
      load_saved_weights(store.engine, USER)[:3] == valid_weights[:3])
check("weights are per-user", load_saved_weights(store.engine, OTHER) is None)

logs = store.review_logs(USER)
check("a ReviewLog is built for every review", len(logs) == store.review_count(USER), str(len(logs)))
check("review_logs carry rating + datetime",
      hasattr(logs[0], "rating") and hasattr(logs[0], "review_datetime"))

# Saving weights invalidates the cached scheduler; the next build uses them.
live = store._schedulers(USER)[0]
check("new weights take effect after save (no restart)",
      list(live.parameters[:3]) == valid_weights[:3], str(live.parameters[:3]))

print("optimizer service")
from gitbook.optimizer import (  # noqa: E402
    OptimizerService, OptimizerUnavailable, optimizer_available,
)

service = OptimizerService(store, SETTINGS)
st = service.status(USER)
check("status reports the review count", st["review_count"] == store.review_count(USER))
check("status exposes the effective-review gate",
      st["required"] == 512 and st["effective_reviews"] >= 0 and "ready" in st)
check("too few effective reviews are not ready", st["ready"] is False, str(st["effective_reviews"]))
check("status marks source 'trained' after saving weights", st["current"]["source"] == "trained")
check("optimizer_available flag matches the real Optimizer",
      st["optimizer_available"] == optimizer_available())
if not optimizer_available():
    try:
        service.run(USER)
        check("run() without the extra raises", False)
    except OptimizerUnavailable:
        check("run() without the extra raises OptimizerUnavailable", True)

print("validation & persistence")
for bad in (0, 5, 6):
    try:
        store.record(USER, "qBad", bad, now=NOW)
        check(f"rating {bad} rejected", False)
    except ValueError:
        check(f"rating {bad} rejected", True)

store.close()
reopened = new_store()
check("data survives a reconnect (persistence)", reopened.get(USER, "qX", NOW)["count"] == 2)
check("FSRS card state survives a reconnect", preview_of(reopened, "qX", NOW)["good"] > 0)
reopened.close()

print()
if failures:
    print(f"{len(failures)} check(s) failed")
    sys.exit(1)
print("all FSRS scheduling & PostgreSQL store checks passed")
