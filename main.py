"""FastAPI app: FSRS-scheduled study over your own cards (authored in the editor).

Cards live in Postgres as ProseMirror documents (see ``content.py``). GitBook is no
longer the source — it survives only as a one-time importer (``POST /api/import/gitbook``).
"""

from __future__ import annotations

import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from content import CardRepository, doc_to_text, markdown_to_doc, render_doc
from gitbook import MarkdownSource, SourceError, get_settings
from gitbook.models import Card
from gitbook.optimizer import NotEnoughReviews, OptimizerService, OptimizerUnavailable
from gitbook.store import open_store

settings = get_settings()
source = MarkdownSource(settings)            # used only by the GitBook importer now
store = open_store(settings)
cards = CardRepository(store.engine)
optimizer = OptimizerService(store, settings)

app = FastAPI(title="Question trainer")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Serve the built React card-manager SPA if it has been built (dev uses Vite directly).
_spa = Path("frontend/dist")
if (_spa / "index.html").exists():
    app.mount("/manage", StaticFiles(directory=str(_spa), html=True), name="manage")

UNCATEGORISED = "Без раздела"
RATINGS = [
    {"value": 1, "key": "again", "label": "Не помню"},
    {"value": 2, "key": "hard", "label": "Тяжело"},
    {"value": 3, "key": "good", "label": "Вспомнил"},
    {"value": 4, "key": "easy", "label": "Легко"},
]
_LONE_P = re.compile(r"^<p>(.*)</p>$", re.DOTALL)


class RandomRequest(BaseModel):
    theme: str | None = None
    subtheme: str | None = None
    answered_only: bool = True
    mode: str = "spaced"
    exclude: list[str] = Field(default_factory=list)


class ReviewRequest(BaseModel):
    question_id: str
    rating: int = Field(ge=1, le=4)


class CardIn(BaseModel):
    question: dict = Field(default_factory=lambda: {"type": "doc", "content": []})
    answer: dict = Field(default_factory=lambda: {"type": "doc", "content": []})
    theme: str = ""
    subtheme: str = ""
    tags: list[str] = Field(default_factory=list)
    position: float | None = None


# ---------------------------------------------------------------- card helpers

def _label(value: str) -> str:
    return value or UNCATEGORISED


def _inline(html: str) -> str:
    """Strip a lone wrapping <p> so a one-line question sits inside the <h1>."""
    match = _LONE_P.match(html.strip())
    return match.group(1) if match else html


def _has_answer(card: Card) -> bool:
    return bool(doc_to_text(card.answer).strip())


def _matches(card: Card, req: RandomRequest) -> bool:
    if req.answered_only and not _has_answer(card):
        return False
    if req.theme and _label(card.theme) != req.theme:
        return False
    if req.subtheme and _label(card.subtheme) != req.subtheme:
        return False
    return True


def _meta(card: Card) -> dict[str, str]:
    return {
        "theme": _label(card.theme),
        "subtheme": card.subtheme or "",
        "section": "",
        "question_text": doc_to_text(card.question),
    }


def _serialise(card: Card, now: datetime) -> dict[str, object]:
    snapshot = store.snapshot(card.id, now)  # progress + preview from one consistent read
    return {
        "id": card.id,
        **_meta(card),
        "question_html": _inline(render_doc(card.question)),
        "answer_html": render_doc(card.answer) if _has_answer(card) else "",
        "has_answer": _has_answer(card),
        "progress": snapshot["progress"],
        "preview": snapshot["preview"],
    }


def _card_summary(card: Card) -> dict:
    return {
        "id": card.id,
        "theme": card.theme,
        "subtheme": card.subtheme,
        "tags": card.tags,
        "question_html": _inline(render_doc(card.question)),
        "question_text": doc_to_text(card.question),
        "has_answer": _has_answer(card),
        "created_at": card.created_at,
        "updated_at": card.updated_at,
    }


def _card_full(card: Card) -> dict:
    return {
        "id": card.id,
        "question": card.question,
        "answer": card.answer,
        "theme": card.theme,
        "subtheme": card.subtheme,
        "tags": card.tags,
        "position": card.position,
        "created_at": card.created_at,
        "updated_at": card.updated_at,
    }


def _pick(pool, schedules, exclude, mode, now):
    """FSRS order drives it; `exclude` (card on screen) prevents a repeat in a row."""
    if mode == "random":
        candidates = [c for c in pool if c.id not in exclude]
        return random.choice(candidates or pool)

    def ordered(candidates):
        due, new, upcoming = [], [], []
        for card in candidates:
            schedule = schedules.get(card.id)
            if schedule is None:
                new.append(card)
            elif schedule["due"] <= now:
                due.append(card)
            else:
                upcoming.append(card)
        due.sort(key=lambda c: schedules[c.id]["due"])
        random.shuffle(new)
        upcoming.sort(key=lambda c: schedules[c.id]["due"])
        return due + new + upcoming

    queue = ordered(pool)
    return next((c for c in queue if c.id not in exclude), queue[0])


# --------------------------------------------------------------------- routes

@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/config")
def get_config():
    return {"ratings": RATINGS, "retention": settings.fsrs_retention, "algorithm": "FSRS"}


@app.get("/api/index")
def get_index():
    """Theme/subtheme tree with counts, for the training filters."""
    pool = cards.all()
    themes: dict[str, dict] = {}
    for card in pool:
        answered = _has_answer(card)
        theme = themes.setdefault(
            _label(card.theme),
            {"name": _label(card.theme), "total": 0, "answered": 0, "subthemes": {}},
        )
        theme["total"] += 1
        theme["answered"] += int(answered)
        if card.subtheme:
            subtheme = theme["subthemes"].setdefault(
                card.subtheme, {"name": card.subtheme, "total": 0, "answered": 0}
            )
            subtheme["total"] += 1
            subtheme["answered"] += int(answered)

    return {
        "total": len(pool),
        "answered": sum(_has_answer(c) for c in pool),
        "themes": [
            {**theme, "subthemes": list(theme["subthemes"].values())}
            for theme in themes.values()
        ],
        "status": {"source": "cards", "stale": False},
    }


@app.post("/api/questions/random")
def random_question(req: RandomRequest):
    pool = [c for c in cards.all() if _matches(c, req)]
    if not pool:
        return JSONResponse(status_code=404, content={"detail": "No cards match the filters."})

    now_epoch = time.time()
    now_dt = datetime.now(timezone.utc)
    schedules = store.schedules()
    card = _pick(pool, schedules, set(req.exclude), req.mode, now_epoch)

    due_count = sum(1 for c in pool if c.id in schedules and schedules[c.id]["due"] <= now_epoch)
    new_count = sum(1 for c in pool if c.id not in schedules)
    return {
        "question": _serialise(card, now_dt),
        "pool_size": len(pool),
        "due_count": due_count,
        "new_count": new_count,
    }


@app.post("/api/reviews")
def record_review(req: ReviewRequest):
    card = cards.get(req.question_id)
    meta = _meta(card) if card else None
    progress = store.record(req.question_id, req.rating, meta=meta)
    return {"progress": progress}


@app.get("/api/stats")
def get_stats():
    live_ids = {c.id for c in cards.all()}
    now = datetime.now(timezone.utc)
    rows = store.stats(now)
    for row in rows:
        row["orphaned"] = row["question_id"] not in live_ids
    return {"rows": rows, "now": now.timestamp()}


# --------------------------------------------------------------- card CRUD

@app.get("/api/cards")
def list_cards(
    theme: str | None = None,
    subtheme: str | None = None,
    search: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    rows = cards.list(theme=theme, subtheme=subtheme, search=search, limit=limit, offset=offset)
    return {"cards": [_card_summary(c) for c in rows], "total": cards.count()}


@app.post("/api/cards", status_code=201)
def create_card(body: CardIn):
    return _card_full(cards.create(body.model_dump()))


@app.get("/api/cards/{card_id}")
def get_card(card_id: str):
    card = cards.get(card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return _card_full(card)


@app.put("/api/cards/{card_id}")
def update_card(card_id: str, body: CardIn):
    card = cards.update(card_id, body.model_dump(exclude_unset=True))
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return _card_full(card)


@app.delete("/api/cards/{card_id}", status_code=204)
def delete_card(card_id: str):
    if not cards.delete(card_id):
        raise HTTPException(status_code=404, detail="Card not found")
    return Response(status_code=204)


# ------------------------------------------------------------ optimizer + import

@app.get("/api/optimizer/status")
def optimizer_status():
    return optimizer.status()


@app.post("/api/optimizer/run")
def optimizer_run():
    try:
        return optimizer.run()
    except OptimizerUnavailable as error:
        raise HTTPException(status_code=501, detail=str(error)) from error
    except NotEnoughReviews as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Optimisation failed: {error}") from error


@app.post("/api/import/gitbook")
def import_gitbook():
    """One-time import: turn the GitBook export into editable cards. Idempotent by text."""
    try:
        questions = source.load(force=True)
    except SourceError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    existing = {(c.theme, doc_to_text(c.question)) for c in cards.all()}
    created = 0
    for question in questions:
        key = (question.theme, question.question_text)
        if key in existing:
            continue
        cards.create({
            "question": markdown_to_doc(question.question),
            "answer": markdown_to_doc(question.body),
            "theme": question.theme,
            "subtheme": question.subtheme,
            "tags": [question.section] if question.section else [],
        })
        existing.add(key)
        created += 1
    return {"imported": created, "total_cards": cards.count()}


@app.post("/api/refresh")
def refresh():
    """Reload the study pool (cards are already live in the DB — this is a no-op count)."""
    return {"total": cards.count(), "status": {"source": "cards", "stale": False}}


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
