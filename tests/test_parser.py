"""Dependency-free checks for the parser and renderer: `python tests/test_parser.py`."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gitbook.parser import parse_questions  # noqa: E402
from gitbook.render import render_answer, render_inline  # noqa: E402

FIXTURE = (Path(__file__).parent / "fixture.md").read_text(encoding="utf-8")
QUESTIONS = parse_questions(FIXTURE)
BY_TEXT = {q.question_text: q for q in QUESTIONS}

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label} {detail}".strip())
        print(f"  FAIL {label} {detail}")


print("headings & structure")
check("top-level questions found", len(QUESTIONS) == 6, f"got {len(QUESTIONS)}")
check(
    "nested details are not top-level questions",
    "Read Committed" not in BY_TEXT,
)

mvcc = BY_TEXT["Что такое MVCC?"]
check("h1 becomes theme", mvcc.theme == "БД", mvcc.theme)
check("h2 becomes subtheme", mvcc.subtheme == "PostgreSQL", mvcc.subtheme)
check("breadcrumb path", mvcc.path == ["БД", "PostgreSQL"], str(mvcc.path))

solid = BY_TEXT["Что такое SOLID?"]
check("h2 resets on a new h1", solid.subtheme == "", solid.subtheme)

kafka = BY_TEXT["Архитектура в Kafka"]
check("later theme is correct", kafka.theme == "Брокеры сообщений", kafka.theme)
check("later subtheme is correct", kafka.subtheme == "Apache Kafka", kafka.subtheme)

print("answers")
check("empty body flagged", BY_TEXT["Что такое KISS?"].has_answer is False)
check("non-empty body flagged", mvcc.has_answer is True)

redis = BY_TEXT["Зачем нужен Redis?"]
check(
    "fenced code containing </details> does not truncate the block",
    "Готово." in redis.body,
)
check("id is stable", redis.id == parse_questions(FIXTURE)[BY_TEXT[
    "Зачем нужен Redis?"].order].id)

print("rendering")
mvcc_html = render_answer(mvcc.body, source_dir="it-database")
check("no GitBook directives leak", "{%" not in mvcc_html, mvcc_html[:120])
check("hint becomes a callout", 'class="gb-callout gb-callout--warning"' in mvcc_html)
check("code title is rendered", 'class="gb-code-title">check.sql<' in mvcc_html)
check("code block survives", "<pre" in mvcc_html and "SELECT xmin" in mvcc_html)
check(
    "relative asset is proxied",
    'src="/asset?path=.gitbook%2Fassets%2Fmvcc.png"' in mvcc_html,
    mvcc_html,
)
check("figcaption preserved", "<figcaption>Схема MVCC</figcaption>" in mvcc_html)

title_html = render_inline(mvcc.question)
check("mark gets a class", 'class="gb-mark gb-mark--primary"' in title_html, title_html)
check("inline render drops the wrapping <p>", not title_html.startswith("<p>"), title_html)

isolation_html = render_answer(BY_TEXT["Уровни изоляции"].body)
check("nested details stay interactive", isolation_html.count('<details class="gb-details"') == 2)
check("nested summary rendered", "<summary>Read Committed</summary>" in isolation_html)
check("nested body rendered", "Самый строгий уровень" in isolation_html)

kafka_html = render_answer(kafka.body)
check("tabs are rendered", kafka_html.count('class="gb-tab"') == 2, kafka_html)
check("tab title kept", 'gb-tab__title">Брокер<' in kafka_html)
check("tab content is markdown", "<p>Хранит партиции.</p>" in kafka_html)
check("table plugin enabled", "<table>" in kafka_html and "<th>Topic</th>" not in kafka_html)
check("table body rendered", "<td>Topic</td>" in kafka_html)

print("heading-level detection")
# The real export wraps everything in one `# Questions` title, so the subjects sit
# at `##` and the sub-subjects at `###`. The invariant levels must be skipped.
WRAPPED = """# Questions

## БД

### PostgreSQL

<details><summary>Что такое MVCC?</summary>Снимок данных.</details>

## DevOps

### Docker

<details><summary>Что такое слой образа?</summary>Слой файловой системы.</details>
"""
wrapped = parse_questions(WRAPPED)
check("document title is skipped", [q.theme for q in wrapped] == ["БД", "DevOps"],
      str([q.theme for q in wrapped]))
check("h3 becomes subtheme when h1 is a title",
      [q.subtheme for q in wrapped] == ["PostgreSQL", "Docker"],
      str([q.subtheme for q in wrapped]))
check("unwrapped documents still map h1/h2", solid.theme == "Принципы написания кода")

print("gitbook edge cases")
# GitBook lets an attribute value span lines; a line-based scan must not leak it.
MULTILINE = '{% code title="# High cohesion\n" %}\n```python\nx = 1\n```\n{% endcode %}'
multiline_html = render_answer(MULTILINE)
check("multi-line directive is expanded", "{%" not in multiline_html, multiline_html[:90])
check("multi-line title is collapsed",
      'gb-code-title"># High cohesion</div>' in multiline_html, multiline_html[:120])

circles = render_answer('<i class="fa-circle-2">:circle-2:</i> Второй пункт')
check("circle shortcode becomes a glyph", "②" in circles and ":circle-" not in circles, circles)

fenced = render_answer("```\nnot a directive: {% code %}\n```")
check("directives inside code fences are untouched", "{% code %}" in fenced, fenced)

print()
if failures:
    print(f"{len(failures)} check(s) failed")
    sys.exit(1)
print(f"all {len(QUESTIONS)} questions parsed, every check passed")
