"""Turns a GitBook markdown export into a flat list of contextualised questions.

Document contract (as authored in GitBook):

    # Theme            -> h1, e.g. "БД", "Брокеры сообщений"
    ## Subtheme        -> h2, e.g. "PostgreSQL", "Apache Kafka"
    ### Section        -> h3, optional extra grouping
    <details>
      <summary>Question</summary>
      ...answer, any markdown/HTML, possibly nested <details>...
    </details>

Every ``<details>`` block becomes one :class:`Question` carrying the headings that
were in effect at that point in the document. Blocks without a body are still
returned, flagged with ``has_answer=False``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterator

FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
DETAILS_OPEN_RE = re.compile(r"<details\b[^>]*>", re.IGNORECASE)
DETAILS_CLOSE_RE = re.compile(r"</details\s*>", re.IGNORECASE)
SUMMARY_RE = re.compile(r"<summary\b[^>]*>(.*?)</summary\s*>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
BREAK_TAG_RE = re.compile(r"</?(br|p|div|li|tr|td|th)\b[^>]*>", re.IGNORECASE)
GITBOOK_TAG_RE = re.compile(r"\{%.*?%\}", re.DOTALL)
MD_NOISE_RE = re.compile(r"[*_`~]+")

# Bodies that exist but say nothing.
PLACEHOLDER_BODIES = {
    "",
    "-",
    "—",
    "todo",
    "tbd",
    "none",
    "пока нет записанного ответа",
    "нет ответа",
}


class Fence:
    """Tracks whether the line stream is currently inside a fenced code block."""

    def __init__(self) -> None:
        self.marker: str | None = None

    def feed(self, line: str) -> bool:
        """Feed one line; return True when the line is a fence or inside a fence."""
        match = FENCE_RE.match(line)
        if match:
            token = match.group(1)
            if self.marker is None:
                self.marker = token
                return True
            if token[0] == self.marker[0] and len(token) >= len(self.marker):
                self.marker = None
                return True
        return self.marker is not None


@dataclass
class Question:
    theme: str
    subtheme: str
    section: str
    question: str
    """Raw summary markdown (may contain inline HTML)."""
    body: str
    """Raw answer markdown (may contain nested <details>)."""
    order: int
    id: str = field(init=False)

    def __post_init__(self) -> None:
        key = f"{self.theme}|{self.subtheme}|{self.section}|{self.question}"
        self.id = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]

    @property
    def question_text(self) -> str:
        return plain_text(self.question)

    @property
    def has_answer(self) -> bool:
        stripped = plain_text(self.body).lower()
        return stripped not in PLACEHOLDER_BODIES

    @property
    def path(self) -> list[str]:
        return [part for part in (self.theme, self.subtheme, self.section) if part]


def plain_text(markdown: str) -> str:
    """Best-effort plain text: drop HTML tags, GitBook directives and md emphasis."""
    text = GITBOOK_TAG_RE.sub(" ", markdown)
    # Inline tags are removed without a separator so `<mark>MVCC</mark>?` stays "MVCC?";
    # line breaks are the exception and must not glue words together.
    text = BREAK_TAG_RE.sub(" ", text)
    text = TAG_RE.sub("", text)
    text = MD_NOISE_RE.sub("", text)
    return " ".join(text.split())


def consume_details(lines: list[str], start: int, fence: Fence) -> tuple[str, int]:
    """Collect a balanced ``<details>`` block starting at ``lines[start]``.

    ``lines[start]`` must already have been fed to ``fence`` and be outside code.
    Returns the raw block and the index of the first line after it.
    """
    line = lines[start]
    depth = len(DETAILS_OPEN_RE.findall(line)) - len(DETAILS_CLOSE_RE.findall(line))
    buffer = [line]
    index = start + 1
    while depth > 0 and index < len(lines):
        line = lines[index]
        buffer.append(line)
        if not fence.feed(line):
            depth += len(DETAILS_OPEN_RE.findall(line))
            depth -= len(DETAILS_CLOSE_RE.findall(line))
        index += 1
    return "\n".join(buffer), index


def split_details(block: str) -> tuple[str, str]:
    """Split a ``<details>`` block into (summary markdown, body markdown)."""
    opening = DETAILS_OPEN_RE.search(block)
    inner = block[opening.end():] if opening else block

    closings = list(DETAILS_CLOSE_RE.finditer(inner))
    if closings:
        inner = inner[: closings[-1].start()]

    summary_match = SUMMARY_RE.search(inner)
    if summary_match:
        # Guard against picking up a nested block's summary when this block has none.
        nested = DETAILS_OPEN_RE.search(inner)
        if nested is None or nested.start() > summary_match.start():
            body = inner[: summary_match.start()] + inner[summary_match.end():]
            return summary_match.group(1).strip(), body.strip("\n")

    return "", inner.strip("\n")


def iter_details_blocks(markdown: str) -> Iterator[tuple[str, dict[int, str]]]:
    """Yield ``(raw block, headings in effect)`` for each top-level details block."""
    lines = markdown.splitlines()
    fence = Fence()
    headings: dict[int, str] = {}
    index = 0

    while index < len(lines):
        line = lines[index]
        if fence.feed(line):
            index += 1
            continue

        heading = HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            headings[level] = plain_text(heading.group(2))
            for deeper in [lv for lv in headings if lv > level]:
                del headings[deeper]
            index += 1
            continue

        if DETAILS_OPEN_RE.search(line):
            block, index = consume_details(lines, index, fence)
            yield block, dict(headings)
            continue

        index += 1


def significant_levels(heading_maps: list[dict[int, str]]) -> list[int]:
    """Work out which heading levels actually carry categories.

    The export wraps the whole document in a single ``# Questions`` title, so the
    subjects live at ``##`` and the sub-subjects at ``###``. A document written
    without that wrapper puts them at ``#`` and ``##``. Rather than hard-coding
    either shape, leading levels that never vary are treated as a page title and
    skipped; the first three levels that remain become theme/subtheme/section.
    """
    levels = sorted({level for headings in heading_maps for level in headings})
    significant: list[int] = []
    for level in levels:
        values = {headings[level] for headings in heading_maps if headings.get(level)}
        if len(values) <= 1 and not significant:
            continue
        significant.append(level)
    return significant


def parse_questions(markdown: str) -> list[Question]:
    """Parse the whole document into questions, in document order."""
    found: list[tuple[str, str, dict[int, str]]] = []
    for block, headings in iter_details_blocks(markdown):
        summary, body = split_details(block)
        if not plain_text(summary):
            # A details block with no summary is not a question.
            continue
        found.append((summary, body, headings))

    levels = significant_levels([headings for _, _, headings in found])

    def at(headings: dict[int, str], depth: int) -> str:
        return headings.get(levels[depth], "") if depth < len(levels) else ""

    return [
        Question(
            theme=at(headings, 0),
            subtheme=at(headings, 1),
            section=at(headings, 2),
            question=summary,
            body=body,
            order=order,
        )
        for order, (summary, body, headings) in enumerate(found)
    ]
