"""FastAPI app serving FSRS-scheduled questions from a GitBook markdown export."""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from gitbook import MarkdownSource, Question, SourceError, get_settings
from gitbook.render import render_answer, render_inline
from gitbook.store import build_scheduler, make_store

settings = get_settings()
source = MarkdownSource(settings)
_live_scheduler, _preview_scheduler = build_scheduler(settings)
store = make_store(settings, _live_scheduler, _preview_scheduler)

app = FastAPI(title="GitBook question trainer")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

UNCATEGORISED = "Без раздела"
RATINGS = [
    {"value": 1, "key": "again", "label": "Не помню"},
    {"value": 2, "key": "hard", "label": "Тяжело"},
    {"value": 3, "key": "good", "label": "Вспомнил"},
    {"value": 4, "key": "easy", "label": "Легко"},
]


class RandomRequest(BaseModel):
    theme: str | None = None
    subtheme: str | None = None
    answered_only: bool = True
    mode: str = "spaced"  # "spaced" (due first) or "random"
    seen: list[str] = Field(default_factory=list)


class ReviewRequest(BaseModel):
    question_id: str
    rating: int = Field(ge=1, le=4)


def _label(value: str) -> str:
    return value or UNCATEGORISED


def _matches(question: Question, req: RandomRequest) -> bool:
    if req.answered_only and not question.has_answer:
        return False
    if req.theme and _label(question.theme) != req.theme:
        return False
    if req.subtheme and _label(question.subtheme) != req.subtheme:
        return False
    return True


def _meta(question: Question) -> dict[str, str]:
    return {
        "theme": _label(question.theme),
        "subtheme": _label(question.subtheme) if question.subtheme else "",
        "section": question.section,
        "question_text": question.question_text,
    }


def _serialise(question: Question, now: datetime) -> dict[str, object]:
    # Single locked read so `progress` and `preview` describe the same card state.
    snapshot = store.snapshot(question.id, now)
    return {
        "id": question.id,
        **_meta(question),
        "question_html": render_inline(question.question, settings.source_dir),
        "answer_html": (
            render_answer(question.body, settings.source_dir) if question.has_answer else ""
        ),
        "has_answer": question.has_answer,
        "progress": snapshot["progress"],
        "preview": snapshot["preview"],
    }


def _load() -> list[Question]:
    try:
        return source.load()
    except SourceError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


def _pick(
    pool: list[Question],
    schedules: dict[str, dict],
    seen: set[str],
    mode: str,
    now: float,
) -> tuple[Question, bool]:
    """Pick the next question. Returns (question, cycled).

    In spaced mode the order is: overdue cards (most overdue first), then
    never-graded cards, then cards scheduled but not yet due. Cards already shown
    this session are skipped until the pool is exhausted, then we cycle
    (``cycled=True``) so a study session never dead-ends.
    """
    if mode == "random":
        fresh = [q for q in pool if q.id not in seen]
        return (random.choice(fresh), False) if fresh else (random.choice(pool), True)

    def ordered(candidates: list[Question]) -> list[Question]:
        due, new, upcoming = [], [], []
        for question in candidates:
            schedule = schedules.get(question.id)
            if schedule is None:
                new.append(question)
            elif schedule["due"] <= now:
                due.append(question)
            else:
                upcoming.append(question)
        due.sort(key=lambda q: schedules[q.id]["due"])       # most overdue first
        random.shuffle(new)                                  # variety among new cards
        upcoming.sort(key=lambda q: schedules[q.id]["due"])  # soonest first
        return due + new + upcoming

    queue = ordered([q for q in pool if q.id not in seen])
    if queue:
        return queue[0], False
    return ordered(pool)[0], True


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/config")
def get_config():
    return {
        "ratings": RATINGS,
        "retention": settings.fsrs_retention,
        "algorithm": "FSRS",
    }


@app.get("/api/index")
def get_index():
    """Theme/subtheme tree with counts, used to populate the filters."""
    questions = _load()
    themes: dict[str, dict] = {}

    for question in questions:
        theme = themes.setdefault(
            _label(question.theme),
            {"name": _label(question.theme), "total": 0, "answered": 0, "subthemes": {}},
        )
        theme["total"] += 1
        theme["answered"] += int(question.has_answer)

        if question.subtheme:
            subtheme = theme["subthemes"].setdefault(
                question.subtheme,
                {"name": question.subtheme, "total": 0, "answered": 0},
            )
            subtheme["total"] += 1
            subtheme["answered"] += int(question.has_answer)

    return {
        "total": len(questions),
        "answered": sum(q.has_answer for q in questions),
        "themes": [
            {**theme, "subthemes": list(theme["subthemes"].values())}
            for theme in themes.values()
        ],
        "status": source.status,
    }


@app.post("/api/questions/random")
def random_question(req: RandomRequest):
    questions = _load()
    pool = [q for q in questions if _matches(q, req)]

    if not pool:
        return JSONResponse(
            status_code=404,
            content={"detail": "No questions match the current filters."},
        )

    now_epoch = time.time()
    now_dt = datetime.now(timezone.utc)
    schedules = store.schedules()
    question, cycled = _pick(pool, schedules, set(req.seen), req.mode, now_epoch)

    due_count = sum(
        1 for q in pool if q.id in schedules and schedules[q.id]["due"] <= now_epoch
    )
    new_count = sum(1 for q in pool if q.id not in schedules)

    return {
        "question": _serialise(question, now_dt),
        "pool_size": len(pool),
        "due_count": due_count,
        "new_count": new_count,
        "cycle_completed": cycled,
    }


@app.post("/api/reviews")
def record_review(req: ReviewRequest):
    """Record a graded review (1–4) and advance the card's FSRS schedule."""
    questions = {q.id: q for q in _load()}
    question = questions.get(req.question_id)
    meta = _meta(question) if question else None
    progress = store.record(req.question_id, req.rating, meta=meta)
    return {"progress": progress}


@app.get("/api/stats")
def get_stats():
    """Every graded card, joined with the live catalogue for orphan detection."""
    live_ids = {q.id for q in _load()}
    now = datetime.now(timezone.utc)
    rows = store.stats(now)
    for row in rows:
        row["orphaned"] = row["question_id"] not in live_ids
    return {"rows": rows, "now": now.timestamp()}


@app.post("/api/refresh")
def refresh():
    try:
        questions = source.load(force=True)
    except SourceError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return {"total": len(questions), "status": source.status}


@app.get("/asset")
def asset(path: str = Query(..., description="Repository-relative asset path")):
    try:
        content, content_type = source.fetch_asset(path)
    except SourceError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return Response(
        content=content,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )
