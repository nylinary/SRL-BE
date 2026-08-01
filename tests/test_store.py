"""Checks for the FSRS store on PostgreSQL: `python tests/test_store.py`.

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

from gitbook.store import ReviewStore  # noqa: E402

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

SCHED = Scheduler(enable_fuzzing=False)
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

failures: list[str] = []
recorded: set[str] = set()


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label} {detail}".strip())
        print(f"  FAIL {label} {detail}")


def new_store() -> ReviewStore:
    return ReviewStore(URL, SCHED, SCHED)


def grade(store: ReviewStore, qid: str, rating: int, when: datetime, **meta) -> dict:
    recorded.add(qid)
    return store.record(qid, rating, meta=meta or None, now=when)


store = new_store()

print(f"target: {DBNAME}")
print("empty state")
check("unknown question has no progress", store.get("qX") is None)
check("schedules start empty", store.schedules() == {})

print("preview (per-rating next interval)")
preview = store.preview("qX", NOW)
check("preview covers all four ratings", set(preview) == {"again", "hard", "good", "easy"})
check(
    "preview is monotonic (Again ≤ Hard ≤ Good ≤ Easy)",
    preview["again"] <= preview["hard"] <= preview["good"] <= preview["easy"],
    str(preview),
)
check("Easy on a new card schedules days out", preview["easy"] > 86400, str(preview["easy"]))

print("recording a review")
p1 = grade(store, "qX", 3, NOW, theme="БД", subtheme="PostgreSQL", question_text="Что такое MVCC?")
check("recording creates progress", store.get("qX", NOW) is not None)
check("count is 1 after one review", p1["count"] == 1, str(p1["count"]))
check("avg equals the only rating", p1["avg_score"] == 3, str(p1["avg_score"]))
check("last_score is the rating", p1["last_score"] == 3, str(p1["last_score"]))
check("next_due is in the future", p1["next_due"] > NOW.timestamp())
check("meta stored for the stats table", p1["question_text"] == "Что такое MVCC?")
check("retrievability is a probability", 0.0 <= p1["retrievability"] <= 1.0, str(p1["retrievability"]))
check("fresh card is in a learning state", p1["state"] in {"learning", "review"}, p1["state"])

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
ids = {row["question_id"] for row in store.stats(NOW)}
check("stats list exactly the graded cards", ids == recorded, f"{ids} vs {recorded}")

sched_map = store.schedules()
check("schedules expose due epochs", "qX" in sched_map and sched_map["qX"]["due"] > 0)

print("validation & persistence")
for bad in (0, 5, 6):
    try:
        store.record("qBad", bad, now=NOW)
        check(f"rating {bad} rejected", False)
    except ValueError:
        check(f"rating {bad} rejected", True)

store.close()
reopened = new_store()
check("data survives a reconnect (persistence)", reopened.get("qX", NOW)["count"] == 2)
check("FSRS card state survives a reconnect", reopened.preview("qX", NOW)["good"] > 0)
reopened.close()

print()
if failures:
    print(f"{len(failures)} check(s) failed")
    sys.exit(1)
print("all FSRS scheduling & PostgreSQL store checks passed")
