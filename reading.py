"""Incremental reading: a per-user tree of documents and extracts.

A *document* holds imported text (TXT/PDF). An *extract* is a selection lifted from its
parent — extracts nest arbitrarily. Cards are created from extracts elsewhere (the answer
defaults to the extract's text). See ``gitbook.models.ReadingItem``.
"""

from __future__ import annotations

import io
import time
import uuid

from sqlmodel import Session, select

from gitbook.models import ReadingItem


class ReadingError(Exception):
    """A bad upload (unsupported type, unreadable file)."""


# ------------------------------------------------------------------- parsing

def parse_upload(filename: str, data: bytes) -> tuple[str, str, str]:
    """(title, text, source_kind) from an uploaded TXT or PDF file."""
    name = (filename or "").strip()
    lower = name.lower()
    title = name.rsplit("/", 1)[-1] or "Document"
    if lower.endswith(".pdf") or data[:5] == b"%PDF-":
        return title, _pdf_text(data), "pdf"
    if lower.endswith(".txt") or lower.endswith(".md") or _looks_like_text(data):
        return title, _decode_text(data), "text"
    raise ReadingError("Unsupported file type — upload a .txt or .pdf.")


def _decode_text(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):   # only trust UTF-16 with a BOM
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "cp1251", "latin-1"):    # cp1251 before latin-1 for Cyrillic
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _looks_like_text(data: bytes) -> bool:
    sample = data[:2048]
    return b"\x00" not in sample  # crude: binary files usually contain NULs


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as error:  # pragma: no cover
        raise ReadingError("PDF support is not installed on the server.") from error
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except Exception as error:
        raise ReadingError(f"Could not read the PDF: {error}") from error
    text = "\n\n".join(p for p in pages if p)
    if not text.strip():
        raise ReadingError("No selectable text found in the PDF (it may be scanned images).")
    return text


# ---------------------------------------------------------------- repository

class ReadingRepository:
    def __init__(self, engine) -> None:
        self.engine = engine

    def _new(self, user_id: str, *, parent_id: str | None, kind: str,
             title: str, content: str, source_kind: str = "") -> ReadingItem:
        now = time.time()
        item = ReadingItem(
            id=uuid.uuid4().hex, user_id=user_id, parent_id=parent_id, kind=kind,
            title=(title or "").strip()[:200], content=content or "",
            source_kind=source_kind, position=now, created_at=now, updated_at=now,
        )
        with Session(self.engine) as session:
            session.add(item)
            session.commit()
            session.refresh(item)
            return item

    def create_document(self, user_id: str, title: str, content: str, source_kind: str) -> ReadingItem:
        return self._new(user_id, parent_id=None, kind="document",
                         title=title or "Untitled", content=content, source_kind=source_kind)

    def create_extract(self, user_id: str, parent_id: str, content: str, title: str = "") -> ReadingItem | None:
        parent = self.get(user_id, parent_id)
        if parent is None:
            return None
        # Default the title to the first line/words of the extract.
        auto = " ".join((content or "").split())[:80]
        return self._new(user_id, parent_id=parent_id, kind="extract",
                         title=title or auto or "Extract", content=content)

    def get(self, user_id: str, item_id: str) -> ReadingItem | None:
        with Session(self.engine) as session:
            item = session.get(ReadingItem, item_id)
        return item if item is not None and item.user_id == user_id else None

    def tree(self, user_id: str) -> list[ReadingItem]:
        """All of the user's items (without heavy content) — the client builds the tree."""
        with Session(self.engine) as session:
            return session.exec(
                select(ReadingItem).where(ReadingItem.user_id == user_id)
                .order_by(ReadingItem.position)
            ).all()

    def update(self, user_id: str, item_id: str, data: dict) -> ReadingItem | None:
        with Session(self.engine) as session:
            item = session.get(ReadingItem, item_id)
            if item is None or item.user_id != user_id:
                return None
            if "title" in data and data["title"] is not None:
                item.title = str(data["title"]).strip()[:200]
            if "content" in data and data["content"] is not None:
                item.content = str(data["content"])
            item.updated_at = time.time()
            session.add(item)
            session.commit()
            session.refresh(item)
            return item

    def delete(self, user_id: str, item_id: str) -> int:
        """Delete an item and its whole subtree. Returns the number removed."""
        with Session(self.engine) as session:
            root = session.get(ReadingItem, item_id)
            if root is None or root.user_id != user_id:
                return 0
            all_items = session.exec(
                select(ReadingItem).where(ReadingItem.user_id == user_id)
            ).all()
            children: dict[str | None, list[ReadingItem]] = {}
            for it in all_items:
                children.setdefault(it.parent_id, []).append(it)
            doomed, stack = [], [root]
            while stack:
                node = stack.pop()
                doomed.append(node)
                stack.extend(children.get(node.id, []))
            for node in doomed:
                session.delete(node)
            session.commit()
            return len(doomed)
