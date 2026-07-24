"""FastAPI app serving random questions from a GitBook markdown export."""

from __future__ import annotations

import random
from typing import Iterable

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from gitbook import MarkdownSource, Question, SourceError, get_settings
from gitbook.render import render_answer, render_inline

settings = get_settings()
source = MarkdownSource(settings)

app = FastAPI(title="GitBook question trainer")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

UNCATEGORISED = "Без раздела"


class RandomRequest(BaseModel):
    theme: str | None = None
    subtheme: str | None = None
    answered_only: bool = True
    seen: list[str] = Field(default_factory=list)


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


def _serialise(question: Question) -> dict[str, object]:
    return {
        "id": question.id,
        "theme": _label(question.theme),
        "subtheme": _label(question.subtheme) if question.subtheme else "",
        "section": question.section,
        "question_html": render_inline(question.question, settings.source_dir),
        "question_text": question.question_text,
        "answer_html": (
            render_answer(question.body, settings.source_dir) if question.has_answer else ""
        ),
        "has_answer": question.has_answer,
    }


def _load() -> list[Question]:
    try:
        return source.load()
    except SourceError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


def _pick(pool: list[Question], seen: Iterable[str]) -> tuple[Question, bool]:
    """Pick a random question, preferring unseen ones. Returns (question, cycled)."""
    seen_ids = set(seen)
    unseen = [q for q in pool if q.id not in seen_ids]
    if unseen:
        return random.choice(unseen), False
    return random.choice(pool), True


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


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

    question, cycled = _pick(pool, req.seen)
    return {
        "question": _serialise(question),
        "pool_size": len(pool),
        "cycle_completed": cycled,
    }


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
