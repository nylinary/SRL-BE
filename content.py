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

from sqlalchemy import func
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


# --------------------------------------------------- HTML -> ProseMirror

_HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_INLINE_MARK_TAGS = {
    "strong": "bold", "b": "bold", "em": "italic", "i": "italic",
    "code": "code", "s": "strike", "del": "strike", "strike": "strike",
}
# Tags that introduce a block; anything else in a block position is a transparent
# container (div/figure/details/section/…) whose children are lifted up.
_BLOCK_TAGS = {"p", "ul", "ol", "li", "pre", "blockquote", "hr", "summary", *_HEADING_TAGS}
_CONTAINER_TAGS = {"div", "figure", "figcaption", "details", "section", "article", "table",
                   "thead", "tbody", "tr", "td", "th"}


def _html_inline(node, marks: list) -> list:
    """Inline nodes (text / hardBreak / image) with the accumulated marks."""
    from bs4 import NavigableString

    if isinstance(node, NavigableString):
        text = str(node)
        if not text:
            return []
        out = {"type": "text", "text": text}
        if marks:
            out["marks"] = list(marks)
        return [out]

    name = (node.name or "").lower()
    if name == "br":
        return [{"type": "hardBreak"}]
    if name == "img":
        return [{"type": "image", "attrs": {
            "src": node.get("src", ""), "alt": node.get("alt") or ""}}]

    marks = list(marks)
    if name in _INLINE_MARK_TAGS:
        mark = {"type": _INLINE_MARK_TAGS[name]}
        if mark not in marks:
            marks.append(mark)
    elif name == "a" and node.get("href"):
        marks.append({"type": "link", "attrs": {"href": node.get("href")}})
    # mark/span/sup/sub/u/… are transparent — keep marks, recurse.

    result = []
    for child in node.children:
        result.extend(_html_inline(child, marks))
    return result


def _html_blocks(parent) -> list:
    """Block nodes for a parent element; loose inline runs become paragraphs."""
    from bs4 import NavigableString

    blocks: list = []
    inline: list = []

    def flush() -> None:
        nonlocal inline
        trimmed = [n for n in inline if not (n.get("type") == "text" and not n["text"].strip())] or inline
        if any(n.get("type") != "text" or n["text"].strip() for n in inline):
            blocks.append({"type": "paragraph", "content": trimmed})
        inline = []

    for child in parent.children:
        if isinstance(child, NavigableString):
            if str(child).strip():
                inline.append({"type": "text", "text": str(child)})
            elif inline:
                inline.append({"type": "text", "text": " "})
            continue

        name = (child.name or "").lower()
        if name in _BLOCK_TAGS:
            flush()
            blocks.append(_html_block_node(child))
        elif name in _CONTAINER_TAGS:
            flush()
            blocks.extend(_html_blocks(child))   # transparent container
        else:
            inline.extend(_html_inline(child, []))   # inline element

    flush()
    return [b for b in blocks if b]


def _html_block_node(node) -> dict | None:
    name = (node.name or "").lower()
    if name == "p":
        return {"type": "paragraph", "content": [n for n in _inline_children(node)]}
    if name in _HEADING_TAGS:
        return {"type": "heading", "attrs": {"level": _HEADING_TAGS[name]},
                "content": _inline_children(node)}
    if name in ("ul", "ol"):
        items = []
        for li in node.find_all("li", recursive=False):
            body = _html_blocks(li) or [{"type": "paragraph"}]
            items.append({"type": "listItem", "content": body})
        return {"type": "orderedList" if name == "ol" else "bulletList", "content": items}
    if name == "pre":
        code = node.find("code")
        text = (code or node).get_text()
        lang = None
        for cls in (code.get("class") if code and code.get("class") else []):
            if cls.startswith("language-"):
                lang = cls[len("language-"):]
        block = {"type": "codeBlock", "content": [{"type": "text", "text": text}] if text else []}
        if lang:
            block["attrs"] = {"language": lang}
        return block
    if name == "blockquote":
        return {"type": "blockquote", "content": _html_blocks(node) or [{"type": "paragraph"}]}
    if name == "hr":
        return {"type": "horizontalRule"}
    if name == "summary":  # nested <details> heading — keep as bold paragraph
        content = _inline_children(node)
        for n in content:
            if n.get("type") == "text":
                n.setdefault("marks", []).append({"type": "bold"})
        return {"type": "paragraph", "content": content}
    return None


def _inline_children(node) -> list:
    out = []
    for child in node.children:
        out.extend(_html_inline(child, []))
    return out


def html_to_doc(html: str) -> dict:
    """HTML → ProseMirror JSON. Pair with the GitBook renderer for a faithful import."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    return {"type": "doc", "content": _html_blocks(soup)}


# --------------------------------------------------------------- persistence

class CardRepository:
    """CRUD over the ``cards`` table, scoped to a single owner (``user_id``)."""

    def __init__(self, engine) -> None:
        self.engine = engine

    def list(
        self,
        user_id: str,
        *,
        theme: str | None = None,
        subtheme: str | None = None,
        search: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Card]:
        with Session(self.engine) as session:
            statement = select(Card).where(Card.user_id == user_id)
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

    def all(self, user_id: str) -> list[Card]:
        with Session(self.engine) as session:
            return session.exec(select(Card).where(Card.user_id == user_id)).all()

    def get(self, user_id: str, card_id: str) -> Card | None:
        """Return the card only if it belongs to this user (else None)."""
        with Session(self.engine) as session:
            card = session.get(Card, card_id)
        return card if card is not None and card.user_id == user_id else None

    def create(self, user_id: str, data: dict) -> Card:
        now = time.time()
        position = data.get("position")
        card = Card(
            id=uuid.uuid4().hex,
            user_id=user_id,
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

    def update(self, user_id: str, card_id: str, data: dict) -> Card | None:
        with Session(self.engine) as session:
            card = session.get(Card, card_id)
            if card is None or card.user_id != user_id:
                return None
            for field in ("question", "answer", "theme", "subtheme", "tags", "position"):
                if field in data and data[field] is not None:
                    setattr(card, field, data[field])
            card.updated_at = time.time()
            session.add(card)
            session.commit()
            session.refresh(card)
            return card

    def delete(self, user_id: str, card_id: str) -> bool:
        with Session(self.engine) as session:
            card = session.get(Card, card_id)
            if card is None or card.user_id != user_id:
                return False
            session.delete(card)
            session.commit()
            return True

    def count(self, user_id: str) -> int:
        with Session(self.engine) as session:
            return session.exec(
                select(func.count()).select_from(Card).where(Card.user_id == user_id)
            ).one()

    def created_since(self, user_id: str, since_epoch: float) -> int:
        """How many cards this user created since ``since_epoch`` — for the daily limit."""
        with Session(self.engine) as session:
            return session.exec(
                select(func.count()).select_from(Card)
                .where(Card.user_id == user_id, Card.created_at >= since_epoch)
            ).one()

    def restore_orphaned(self, user_id: str) -> dict:
        """Recreate cards for this user's study history that has no matching card anymore.

        When cards were removed from the source and wiped on a re-import, their FSRS
        history (``progress`` rows) was left dangling — the stats screen shows those as
        "removed from source". This rebuilds a card for each, **reusing the progress'
        ``question_id`` as the new card id** so the history reconnects automatically.

        Additive and safe: never touches an existing card, and skips any history whose
        question text already exists as a card (so a question that WAS re-imported under a
        new id isn't duplicated). Answers can't be recovered — only the question text
        survived in the history — so restored cards start with an empty answer. Idempotent.
        """
        from gitbook.models import Progress

        now = time.time()
        with Session(self.engine) as session:
            cards = session.exec(select(Card).where(Card.user_id == user_id)).all()
            live_ids = {c.id for c in cards}
            seen_text = {doc_to_text(c.question).strip().lower() for c in cards}
            rows = session.exec(select(Progress).where(Progress.user_id == user_id)).all()

            restored, skipped = 0, 0
            for p in rows:
                if p.question_id in live_ids:
                    continue  # history already has its card
                text = (p.question_text or "").strip()
                if not text:
                    continue
                if text.lower() in seen_text:
                    skipped += 1  # this question already exists under another card
                    continue
                session.add(Card(
                    id=p.question_id, user_id=user_id,
                    question=text_to_doc(text), answer={"type": "doc", "content": []},
                    theme=p.theme or "", subtheme=p.subtheme or "", tags=[],
                    position=now, created_at=now, updated_at=now,
                ))
                seen_text.add(text.lower())
                live_ids.add(p.question_id)
                restored += 1
            session.commit()
            return {"restored": restored, "skipped_duplicates": skipped}
