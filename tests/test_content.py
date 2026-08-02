"""Pure-function checks for the card content core: `python tests/test_content.py`.

No database needed — CRUD is covered by the API end-to-end run.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from content import (  # noqa: E402
    doc_to_text, html_to_doc, markdown_to_doc, render_doc, text_to_doc,
)
from gitbook.render import render_answer, render_inline  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label} {detail}".strip())
        print(f"  FAIL {label} {detail}")


DOC = {"type": "doc", "content": [
    {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "MVCC"}]},
    {"type": "paragraph", "content": [
        {"type": "text", "text": "Multi", "marks": [{"type": "bold"}]},
        {"type": "text", "text": "version "},
        {"type": "text", "text": "x", "marks": [{"type": "italic"}]},
        {"type": "text", "text": "y", "marks": [{"type": "code"}]},
    ]},
    {"type": "codeBlock", "attrs": {"language": "sql"},
     "content": [{"type": "text", "text": "SELECT xmin FROM t;"}]},
    {"type": "bulletList", "content": [
        {"type": "listItem", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "snapshot"}]}]}]},
    {"type": "blockquote", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "note"}]}]},
]}

print("render_doc (ProseMirror -> HTML)")
html = render_doc(DOC)
check("heading", "<h2>MVCC</h2>" in html)
check("bold mark", "<strong>Multi</strong>" in html)
check("italic mark", "<em>x</em>" in html)
check("inline code mark", "<code>y</code>" in html)
check("code block with language class",
      '<pre><code class="language-sql">SELECT xmin FROM t;</code></pre>' in html, html)
check("bullet list", "<ul><li><p>snapshot</p></li></ul>" in html)
check("blockquote", "<blockquote><p>note</p></blockquote>" in html)

print("render_doc escapes HTML")
danger = {"type": "doc", "content": [
    {"type": "paragraph", "content": [{"type": "text", "text": "<script>alert(1)</script>"}]}]}
out = render_doc(danger)
check("text is escaped, no raw tags", "<script>" not in out and "&lt;script&gt;" in out, out)

print("doc_to_text")
text = doc_to_text(DOC)
check("collects text across nodes", "MVCC" in text and "SELECT xmin" in text and "snapshot" in text)
check("empty doc -> empty text", doc_to_text({"type": "doc", "content": []}) == "")
check("text_to_doc round-trips", doc_to_text(text_to_doc("hello")) == "hello")

print("markdown_to_doc (GitBook import)")
pm = markdown_to_doc("## Title\n\nSome **bold** & `code`.\n\n```python\nprint(1)\n```\n\n- a\n- b\n")
back = render_doc(pm)
check("heading imported", "<h2>Title</h2>" in back, back[:120])
check("bold imported", "<strong>bold</strong>" in back)
check("code fence keeps language", 'class="language-python"' in back)
check("list imported", "<li><p>a</p></li>" in back and "<li><p>b</p></li>" in back)
check("ampersand escaped once", "&amp;" in back and "&amp;amp;" not in back, back)

print("html_to_doc (GitBook import path: render -> HTML -> ProseMirror)")
q_out = render_doc(html_to_doc(render_inline(
    'Что такое <mark style="color:yellow;"><strong><code>MRO</code></strong></mark>?')))
check("no raw/escaped tags leak from the question", "<mark" not in q_out and "&lt;" not in q_out, q_out)
check("mark unwrapped, bold+code kept", "<strong>" in q_out and "<code>" in q_out and "MRO" in q_out, q_out)

a_out = render_doc(html_to_doc(render_answer(
    "## Заголовок\n\nТекст **жир** и `код`.\n\n```python\nprint(1)\n```\n\n- a\n- b\n")))
check("no leaked HTML / directives in the answer", "&lt;" not in a_out and "{%" not in a_out, a_out[:120])
check("heading imported", "<h2>Заголовок</h2>" in a_out)
check("code block keeps language", 'class="language-python"' in a_out and "print(1)" in a_out)
check("marks imported", "<strong>жир</strong>" in a_out and "<code>код</code>" in a_out)
check("list imported", "<li><p>a</p></li>" in a_out and "<li><p>b</p></li>" in a_out)

print()
if failures:
    print(f"{len(failures)} check(s) failed")
    sys.exit(1)
print("all content-core checks passed")
