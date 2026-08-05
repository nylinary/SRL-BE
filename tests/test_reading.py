"""Incremental-reading checks: `python tests/test_reading.py`.

Parsing is DB-free; the repository part targets ``TEST_DATABASE_URL`` (name must contain
"test") and SKIPs if no PostgreSQL is reachable.
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reading import ReadingError, parse_upload  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        failures.append(label)


print("parse_upload")
title, text, kind = parse_upload("notes.txt", "hello\nworld".encode("utf-8"))
check("txt parsed", kind == "text" and text == "hello\nworld" and title == "notes.txt")
title, text, kind = parse_upload("к.txt", "привет".encode("cp1251"))
check("cp1251 decoded", text == "привет", text)
try:
    parse_upload("a.bin", b"\x00\x01\x02binary")
    check("binary rejected", False)
except ReadingError:
    check("binary rejected", True)

# --- repository (needs Postgres) ---
import psycopg  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL", f"postgresql://{getpass.getuser()}@127.0.0.1:5432/qt_test")
DBNAME = URL.rsplit("/", 1)[-1].split("?")[0]
if "test" not in DBNAME.lower():
    print(f"SKIP repo: '{DBNAME}' is not a test database.")
    sys.exit(1 if failures else 0)
try:
    psycopg.connect(URL).close()
except Exception as error:
    print(f"SKIP repo: PostgreSQL unavailable ({type(error).__name__}).")
    sys.exit(1 if failures else 0)

with psycopg.connect(URL, autocommit=True) as conn:
    conn.execute("DROP TABLE IF EXISTS reading_items")

from gitbook.models import make_engine  # noqa: E402
from reading import ReadingRepository  # noqa: E402

repo = ReadingRepository(make_engine(URL))
U = "reader-test"

print("repository tree")
doc = repo.create_document(U, "Doc", "Alpha.\n\nBeta.\n\nGamma.", "text")
check("document created", doc.kind == "document" and doc.parent_id is None)
ex = repo.create_extract(U, doc.id, "Beta.")
check("extract child of doc", ex.parent_id == doc.id and ex.kind == "extract")
check("extract auto-titled", ex.title == "Beta.", ex.title)
sub = repo.create_extract(U, ex.id, "Bet")
check("sub-extract nests", sub.parent_id == ex.id)
check("cross-user isolation", repo.get("someone-else", doc.id) is None)
check("tree lists all three", len(repo.tree(U)) == 3)

pdfdoc = repo.create_document(U, "PDF", "extracted text", "pdf", blob=b"%PDF-1.4 fake")
check("blob stored", repo.get_blob(U, pdfdoc.id) == b"%PDF-1.4 fake")
check("blob cross-user isolation", repo.get_blob("someone-else", pdfdoc.id) is None)
check("blob removed with item", repo.delete(U, pdfdoc.id) == 1 and repo.get_blob(U, pdfdoc.id) is None)

updated = repo.update(U, ex.id, {"title": "Renamed"})
check("update renames", updated.title == "Renamed")

removed = repo.delete(U, doc.id)
check("delete cascades subtree", removed == 3 and len(repo.tree(U)) == 0)

print()
if failures:
    print(f"{len(failures)} check(s) failed")
    sys.exit(1)
print("all reading checks passed")
