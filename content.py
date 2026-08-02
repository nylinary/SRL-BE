"""The card content core: ProseMirror/TipTap documents in, HTML/plain-text out.

``question`` and ``answer`` are stored as ProseMirror JSON — the format the TipTap
editor produces and the one portable format every client can share (web today, a
browser extension and React Native later). This module is the single server-side
place that understands that schema:

- :func:`render_doc` — ProseMirror JSON → HTML (for the study view and list previews)
- :func:`doc_to_text` — ProseMirror JSON → plain text (search, "has answer?")
- :func:`markdown_to_doc` — Markdown → ProseMirror JSON (the one-time GitBook import)
- :class:`CardRepository` — CRUD over the ``cards`` table
"""

from __future__ import annotations

import html as _html
import time
import uuid

from sqlmodel import Session, select

from gitbook.models import Card

# ------------------------------------------------------------------- rendering

_MARK_TAGS = {"bold": "strong", "italic": "em", "code": "code", "strike": "s"}


def text_to_doc(text: str) -> dict:
    """A minimal ProseMirror doc wrapping a single plain-text paragraph."""
    if not text:
        return {"type": "doc", "content": []}
    return {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": text}]}
    ]}


def _node_text(node: dict) -> str:
    if node.get("type") == "text":
        return node.get("text", "")
    return "".join(_node_text(child) for child in node.get("content", []))


def _render_text(node: dict) -> str:
    text = _html.escape(node.get("text", ""))
    for mark in node.get("marks", []):
        kind = mark.get("type")
        if kind in _MARK_TAGS:
            tag = _MARK_TAGS[kind]
            text = f"<{tag}>{text}</{tag}>"
        elif kind == "link":
            href = _html.escape(mark.get("attrs", {}).get("href", ""), quote=True)
            text = f'<a href="{href}" target="_blank" rel="noopener">{text}</a>'
    return text


def _render_node(node: dict) -> str:
    kind = node.get("type")
    if kind == "text":
        return _render_text(node)

    attrs = node.get("attrs", {})
    if kind == "codeBlock":
        lang = attrs.get("language") or ""
        cls = f' class="language-{_html.escape(lang, quote=True)}"' if lang else ""
        return f"<pre><code{cls}>{_html.escape(_node_text(node))}</code></pre>"
    if kind == "horizontalRule":
        return "<hr>"
    if kind == "hardBreak":
        return "<br>"
    if kind == "image":
        src = _html.escape(attrs.get("src", ""), quote=True)
        alt = _html.escape(attrs.get("alt") or "", quote=True)
        return f'<img src="{src}" alt="{alt}">'

    children = "".join(_render_node(child) for child in node.get("content", []))
    if kind == "paragraph":
        return f"<p>{children}</p>"
    if kind == "heading":
        level = min(max(int(attrs.get("level", 1)), 1), 6)
        return f"<h{level}>{children}</h{level}>"
    if kind == "bulletList":
        return f"<ul>{children}</ul>"
    if kind == "orderedList":
        return f"<ol>{children}</ol>"
    if kind == "listItem":
        return f"<li>{children}</li>"
    if kind == "blockquote":
        return f"<blockquote>{children}</blockquote>"
    return children  # doc or unknown wrapper — just emit children


def render_doc(doc: dict | None) -> str:
    if not doc:
        return ""
    return "".join(_render_node(child) for child in doc.get("content", []))


_BLOCK_TYPES = {
    "paragraph", "heading", "codeBlock", "blockquote", "listItem", "horizontalRule",
}


def doc_to_text(doc: dict | None) -> str:
    """Plain text with block boundaries as newlines — for search and emptiness checks."""
    if not doc:
        return ""

    parts: list[str] = []

    def walk(node: dict) -> None:
        if node.get("type") == "text":
            parts.append(node.get("text", ""))
            return
        for child in node.get("content", []):
            walk(child)
        if node.get("type") in _BLOCK_TYPES:
            parts.append("\n")

    walk(doc)
    return " ".join("".join(parts).split())


# ---------------------------------------------------- markdown -> ProseMirror

def _inline_tokens_to_pm(tokens: list) -> list:
    out: list = []
    for tok in tokens or []:
        kind = tok.get("type")
        if kind == "text":
            out.append({"type": "text", "text": tok.get("raw", "")})
        elif kind in ("strong", "emphasis", "codespan", "del", "link"):
            if kind == "codespan":
                out.append({"type": "text", "text": tok.get("raw", ""),
                            "marks": [{"type": "code"}]})
                continue
            mark = {"strong": "bold", "emphasis": "italic", "del": "strike",
                    "link": "link"}[kind]
            children = _inline_tokens_to_pm(tok.get("children", []))
            for child in children:
                marks = child.setdefault("marks", [])
                if mark == "link":
                    marks.append({"type": "link",
                                  "attrs": {"href": tok.get("attrs", {}).get("url", "")}})
                else:
                    marks.append({"type": mark})
            out.extend(children)
        elif kind in ("linebreak", "softbreak"):
            out.append({"type": "text", "text": " "})
        elif tok.get("children"):
            out.extend(_inline_tokens_to_pm(tok["children"]))
        elif tok.get("raw"):
            out.append({"type": "text", "text": tok["raw"]})
    return out


def _block_token_to_pm(tok: dict) -> dict | None:
    kind = tok.get("type")
    if kind == "heading":
        return {"type": "heading", "attrs": {"level": tok.get("attrs", {}).get("level", 1)},
                "content": _inline_tokens_to_pm(tok.get("children", []))}
    if kind == "paragraph":
        return {"type": "paragraph", "content": _inline_tokens_to_pm(tok.get("children", []))}
    if kind == "block_code":
        lang = (tok.get("attrs", {}).get("info") or "").split()[:1]
        node = {"type": "codeBlock", "content": [{"type": "text", "text": tok.get("raw", "")}]}
        if lang:
            node["attrs"] = {"language": lang[0]}
        return node
    if kind == "block_quote":
        return {"type": "blockquote", "content": _blocks_to_pm(tok.get("children", []))}
    if kind == "list":
        list_type = "orderedList" if tok.get("attrs", {}).get("ordered") else "bulletList"
        items = []
        for item in tok.get("children", []):
            items.append({"type": "listItem", "content": _blocks_to_pm(item.get("children", []))})
        return {"type": list_type, "content": items}
    if kind == "thematic_break":
        return {"type": "horizontalRule"}
    if kind in ("blank_line", "block_html"):
        return None
    if tok.get("children"):  # unknown block with children — treat as paragraph
        return {"type": "paragraph", "content": _inline_tokens_to_pm(tok["children"])}
    return None


def _blocks_to_pm(tokens: list) -> list:
    out = []
    for tok in tokens or []:
        node = _block_token_to_pm(tok)
        if node:
            out.append(node)
    return out


def markdown_to_doc(markdown: str) -> dict:
    """Best-effort Markdown → ProseMirror JSON, used only by the GitBook importer."""
    import mistune

    tokens = mistune.create_markdown(renderer=None)(markdown or "")
    content = _blocks_to_pm(tokens)
    return {"type": "doc", "content": content or []}


# --------------------------------------------------------------- persistence

class CardRepository:
    """CRUD over the ``cards`` table, sharing the store's engine."""

    def __init__(self, engine) -> None:
        self.engine = engine

    def list(
        self,
        *,
        theme: str | None = None,
        subtheme: str | None = None,
        search: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Card]:
        with Session(self.engine) as session:
            statement = select(Card)
            if theme:
                statement = statement.where(Card.theme == theme)
            if subtheme:
                statement = statement.where(Card.subtheme == subtheme)
            statement = statement.order_by(Card.position, Card.created_at)
            rows = session.exec(statement).all()
        if search:
            needle = search.lower()
            rows = [c for c in rows if needle in doc_to_text(c.question).lower()
                    or needle in doc_to_text(c.answer).lower()]
        return rows[offset:offset + limit]

    def all(self) -> list[Card]:
        with Session(self.engine) as session:
            return session.exec(select(Card)).all()

    def get(self, card_id: str) -> Card | None:
        with Session(self.engine) as session:
            return session.get(Card, card_id)

    def create(self, data: dict) -> Card:
        now = time.time()
        position = data.get("position")
        card = Card(
            id=uuid.uuid4().hex,
            question=data.get("question") or {"type": "doc", "content": []},
            answer=data.get("answer") or {"type": "doc", "content": []},
            theme=(data.get("theme") or "").strip(),
            subtheme=(data.get("subtheme") or "").strip(),
            tags=list(data.get("tags") or []),
            position=float(position) if position is not None else now,
            created_at=now,
            updated_at=now,
        )
        with Session(self.engine) as session:
            session.add(card)
            session.commit()
            session.refresh(card)
            return card

    def update(self, card_id: str, data: dict) -> Card | None:
        with Session(self.engine) as session:
            card = session.get(Card, card_id)
            if card is None:
                return None
            for field in ("question", "answer", "theme", "subtheme", "tags", "position"):
                if field in data and data[field] is not None:
                    setattr(card, field, data[field])
            card.updated_at = time.time()
            session.add(card)
            session.commit()
            session.refresh(card)
            return card

    def delete(self, card_id: str) -> bool:
        with Session(self.engine) as session:
            card = session.get(Card, card_id)
            if card is None:
                return False
            session.delete(card)
            session.commit()
            return True

    def count(self) -> int:
        return len(self.all())
