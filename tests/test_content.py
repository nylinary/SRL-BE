"""Pure-function checks for the card content core: `python tests/test_content.py`.

No database needed — CRUD is covered by the API end-to-end run.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from content import doc_to_text, markdown_to_doc, render_doc, text_to_doc  # noqa: E402

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

print()
if failures:
    print(f"{len(failures)} check(s) failed")
    sys.exit(1)
print("all content-core checks passed")
