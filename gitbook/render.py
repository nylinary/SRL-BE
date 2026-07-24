"""Renders GitBook-flavoured markdown to HTML.

GitBook adds a handful of ``{% ... %}`` directives and colour-coded ``<mark>`` tags
on top of CommonMark. Plain markdown parsers leak those as literal text, which is
what made the old output look broken. Here they are expanded into semantic markup
that ``static/styles.css`` knows how to style.
"""

from __future__ import annotations

import posixpath
import re
from urllib.parse import quote

import mistune

from .parser import DETAILS_OPEN_RE, Fence, consume_details, split_details

_markdown = mistune.create_markdown(
    escape=False,
    plugins=["table", "strikethrough", "url", "task_lists", "footnotes", "def_list"],
)

DIRECTIVE_RE = re.compile(r"\{%\s*(?P<body>.*?)\s*%\}", re.DOTALL)
ATTR_RE = re.compile(r"(?P<key>[\w-]+)\s*=\s*\"(?P<value>[^\"]*)\"")
MARK_RE = re.compile(r"<mark\b([^>]*)>", re.IGNORECASE)
COLOR_RE = re.compile(r"color:\s*\$?([a-zA-Z][\w-]*)", re.IGNORECASE)
IMG_SRC_RE = re.compile(r"(<img\b[^>]*?\bsrc=\")([^\"]+)(\")", re.IGNORECASE)
EXTERNAL_LINK_RE = re.compile(r"<a\s+href=\"(https?://[^\"]+)\"", re.IGNORECASE)
LEADING_P_RE = re.compile(r"^<p>(.*)</p>$", re.DOTALL)

HINT_STYLES = {"info", "success", "warning", "danger"}

# GitBook's numbered-circle shortcodes, which no markdown parser knows about.
CIRCLE_RE = re.compile(r"(?<![\w:]):circle-([0-9]):(?!\w)")
CIRCLES = "⓪①②③④⑤⑥⑦⑧⑨"


def _attrs(text: str) -> dict[str, str]:
    return {m.group("key"): m.group("value") for m in ATTR_RE.finditer(text)}


def _expand_directive(body: str) -> list[str]:
    """Expand one ``{% ... %}`` directive into raw HTML lines.

    Blank lines are emitted around the wrappers on purpose: CommonMark only parses
    markdown inside a raw HTML block once a blank line has closed the block.
    """
    name = body.split(None, 1)[0].lower() if body.split() else ""
    attrs = _attrs(body)

    if name == "hint":
        style = attrs.get("style", "info").lower()
        if style not in HINT_STYLES:
            style = "info"
        return [f'<div class="gb-callout gb-callout--{style}">', ""]
    if name == "endhint":
        return ["", "</div>"]

    if name == "code":
        title = " ".join(attrs.get("title", "").split())
        if not title:
            return []
        # The trailing blank line closes the raw HTML block, otherwise the fenced
        # code block that follows would be swallowed into it as literal text.
        return [f'<div class="gb-code-title">{mistune.escape(title)}</div>', ""]
    if name == "endcode":
        return []

    if name == "tabs":
        return ['<div class="gb-tabs">', ""]
    if name == "endtabs":
        return ["", "</div>"]
    if name == "tab":
        title = mistune.escape(attrs.get("title", "Tab"))
        return ['<div class="gb-tab">', f'<div class="gb-tab__title">{title}</div>', ""]
    if name == "endtab":
        return ["", "</div>"]

    if name == "stepper":
        return ['<ol class="gb-stepper">', ""]
    if name == "endstepper":
        return ["", "</ol>"]
    if name == "step":
        return ['<li class="gb-step">', ""]
    if name == "endstep":
        return ["", "</li>"]

    if name == "columns":
        return ['<div class="gb-columns">', ""]
    if name == "endcolumns":
        return ["", "</div>"]
    if name == "column":
        return ['<div class="gb-column">', ""]
    if name == "endcolumn":
        return ["", "</div>"]

    if name in {"embed", "file"}:
        target = attrs.get("url") or attrs.get("src", "")
        if not target:
            return []
        safe = mistune.escape(target)
        return [
            "",
            f'<p class="gb-embed"><a href="{safe}" target="_blank" rel="noopener">{safe}</a></p>',
            "",
        ]

    # content-ref wraps a plain markdown link — keep the link, drop the wrapper.
    # Anything else unknown is dropped rather than leaked as literal text.
    return []


def _preprocess(markdown: str) -> str:
    """Rewrite GitBook directives, leaving fenced code blocks untouched."""
    output: list[str] = []
    fence = Fence()
    pending: list[str] = []

    for line in markdown.splitlines():
        if pending:
            # A directive whose attribute value contains a newline, e.g.
            #   {% code title="something
            #   " %}
            pending.append(line)
            joined = "\n".join(pending)
            if "%}" not in line:
                continue
            pending = []
            directive = DIRECTIVE_RE.fullmatch(joined.strip())
            output.extend(_expand_directive(directive.group("body")) if directive else [joined])
            continue

        if fence.feed(line):
            output.append(line)
            continue

        if ":circle-" in line:
            line = CIRCLE_RE.sub(lambda m: CIRCLES[int(m.group(1))], line)

        stripped = line.strip()
        if stripped.startswith("{%") and "%}" not in stripped:
            pending.append(line)
            continue

        whole_line = DIRECTIVE_RE.fullmatch(stripped)
        if whole_line:
            output.extend(_expand_directive(whole_line.group("body")))
            continue

        # Inline leftovers (e.g. two directives on one line) are stripped in place.
        output.append(DIRECTIVE_RE.sub("", line) if "{%" in line else line)

    output.extend(pending)  # unterminated directive — keep the text rather than eat it
    return "\n".join(output)


def _rewrite_mark(match: re.Match[str]) -> str:
    attrs = match.group(1) or ""
    color = COLOR_RE.search(attrs)
    if not color:
        return '<mark class="gb-mark">'
    return f'<mark class="gb-mark gb-mark--{color.group(1).lower()}">'


def _asset_url(src: str, source_dir: str) -> str:
    if re.match(r"^(https?:|data:|//|/)", src, re.IGNORECASE):
        return src
    resolved = posixpath.normpath(posixpath.join(source_dir, src)).lstrip("/")
    return f"/asset?path={quote(resolved, safe='')}"


def _postprocess(html: str, source_dir: str) -> str:
    html = MARK_RE.sub(_rewrite_mark, html)
    html = IMG_SRC_RE.sub(
        lambda m: f"{m.group(1)}{_asset_url(m.group(2), source_dir)}{m.group(3)}", html
    )
    html = EXTERNAL_LINK_RE.sub(r'<a target="_blank" rel="noopener" href="\1"', html)
    return html


def render_markdown(markdown: str, source_dir: str = "") -> str:
    """Render a markdown fragment (no nested ``<details>`` handling)."""
    if not markdown.strip():
        return ""
    return _postprocess(_markdown(_preprocess(markdown)), source_dir)


def render_inline(markdown: str, source_dir: str = "") -> str:
    """Render a short fragment (a question title) without the wrapping ``<p>``."""
    html = render_markdown(markdown, source_dir).strip()
    match = LEADING_P_RE.match(html)
    return match.group(1).strip() if match else html


def render_answer(markdown: str, source_dir: str = "") -> str:
    """Render an answer body, keeping nested ``<details>`` blocks interactive."""
    lines = markdown.splitlines()
    fence = Fence()
    parts: list[str] = []
    buffer: list[str] = []
    index = 0

    def flush() -> None:
        if buffer:
            parts.append(render_markdown("\n".join(buffer), source_dir))
            buffer.clear()

    while index < len(lines):
        line = lines[index]
        if not fence.feed(line) and DETAILS_OPEN_RE.search(line):
            flush()
            block, index = consume_details(lines, index, fence)
            summary, body = split_details(block)
            parts.append(
                '<details class="gb-details">'
                f'<summary>{render_inline(summary, source_dir) or "Подробнее"}</summary>'
                f'<div class="gb-details__body">{render_answer(body, source_dir)}</div>'
                "</details>"
            )
            continue
        buffer.append(line)
        index += 1

    flush()
    return "\n".join(part for part in parts if part.strip())
