"""FastAPI app: multi-user, FSRS-scheduled study over each user's own cards.

Every user signs in with a social provider (Google/GitHub/Yandex/VK), gets a JWT,
and sees only their own cards, schedule, history, and trained weights. Cards are
ProseMirror documents (see ``content.py``). GitBook is gone except for ``/asset``,
which still proxies images referenced by already-imported cards.
"""

from __future__ import annotations

import random
import re
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, HTTPException, Query, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from auth import oauth
from auth.deps import bind_users, get_current_user, require_admin
from auth.oauth import OAuthError
from auth.tokens import AuthError, issue_session, issue_state, read_state
from auth.users import BadCredentials, EmailTaken, UserRepository
from content import CardRepository, doc_to_text, html_to_doc, render_doc
from gitbook import MarkdownSource, SourceError, get_settings
from gitbook.models import Card, ReadingItem, User
from gitbook.optimizer import NotEnoughReviews, OptimizerService, OptimizerUnavailable
from gitbook.render import render_answer, render_inline
from gitbook.store import open_store
from reading import ReadingError, ReadingRepository

settings = get_settings()
source = MarkdownSource(settings)            # used only by the /asset image proxy now
store = open_store(settings)
cards = CardRepository(store.engine)
optimizer = OptimizerService(store, settings)
reading = ReadingRepository(store.engine)
users = UserRepository(store.engine)
bind_users(users)
# One-time (idempotent) cleanup: normalise stored emails and merge accounts that are the
# same mailbox — e.g. Gmail dot/alias variants registered before canonicalization existed.
users.dedupe_by_email()

app = FastAPI(title="SRL API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

UNCATEGORISED = "Без раздела"
RATINGS = [
    {"value": 1, "key": "again", "label": "Again"},
    {"value": 2, "key": "hard", "label": "Hard"},
    {"value": 3, "key": "good", "label": "Good"},
    {"value": 4, "key": "easy", "label": "Easy"},
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


class RegisterRequest(BaseModel):
    email: str
    password: str
    name: str = ""


class LoginRequest(BaseModel):
    email: str
    password: str


class CardIn(BaseModel):
    question: dict = Field(default_factory=lambda: {"type": "doc", "content": []})
    answer: dict = Field(default_factory=lambda: {"type": "doc", "content": []})
    theme: str = ""
    subtheme: str = ""
    tags: list[str] = Field(default_factory=list)
    position: float | None = None


class ReadingDocIn(BaseModel):
    title: str = ""
    content: str


class ExtractIn(BaseModel):
    content: str
    title: str = ""


class ReadingUpdate(BaseModel):
    title: str | None = None
    content: str | None = None


class MakeCardRequest(BaseModel):
    question: str = ""
    answer: str | None = None      # defaults to the extract's text when omitted
    theme: str = ""
    subtheme: str = ""
    tags: list[str] = Field(default_factory=list)


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


def _serialise(card: Card, now: datetime, user_id: str) -> dict[str, object]:
    snapshot = store.snapshot(user_id, card.id, now)  # progress + preview, one read
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


def _user_public(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "avatar_url": user.avatar_url,
        "provider": user.provider,
        "is_admin": user.is_admin,
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


# --------------------------------------------------------------------- auth

@app.get("/")
def root():
    return {"service": "SRL API", "docs": "/docs"}


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _session_for(user: User) -> str:
    """Issue a session token and, for admins, adopt any pre-multi-user orphan rows.

    Auto-claim is idempotent: once claimed there are no orphans left, so repeat logins
    are no-ops. It's how the original single-user data lands in the admin's account.
    """
    if user.is_admin:
        users.claim_orphans(user.id)
    return issue_session(user_id=user.id, email=user.email, is_admin=user.is_admin)


@app.get("/api/auth/providers")
def auth_providers():
    """Which social logins are configured on this deployment."""
    return {"providers": oauth.available_providers()}


@app.post("/api/auth/register", status_code=201)
def auth_register(body: RegisterRequest):
    email = body.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    try:
        user = users.register_password(email, body.password, body.name.strip())
    except EmailTaken as error:
        raise HTTPException(status_code=409, detail="This email is already registered.") from error
    return {"token": _session_for(user), "user": _user_public(user)}


@app.post("/api/auth/login")
def auth_login_password(body: LoginRequest):
    try:
        user = users.authenticate_password(body.email, body.password)
    except BadCredentials as error:
        raise HTTPException(status_code=401, detail="Wrong email or password.") from error
    return {"token": _session_for(user), "user": _user_public(user)}


def _valid_ext_redirect(url: str | None) -> bool:
    """Only allow bouncing the token to a Chrome extension's own redirect URL."""
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.scheme == "https" and bool(parsed.hostname) and parsed.hostname.endswith(".chromiumapp.org")


def _append(base: str, key: str, value: str) -> str:
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{key}={value}"


@app.get("/api/auth/{provider}/login")
def auth_login(provider: str, redirect: str | None = None):
    if provider not in oauth.available_providers():
        raise HTTPException(status_code=404, detail="Provider not available")
    # `redirect` lets the browser extension receive the token at its chromiumapp.org URL
    # instead of the web app. It's carried (signed) in the state and validated on return.
    dest = redirect if _valid_ext_redirect(redirect) else None
    state = issue_state(provider, secrets.token_urlsafe(16), dest)
    try:
        return RedirectResponse(oauth.authorize_url(provider, state))
    except OAuthError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.get("/api/auth/{provider}/callback")
def auth_callback(provider: str, code: str | None = None, state: str | None = None,
                  error: str | None = None):
    front = settings.frontend_url or ""
    dest = f"{front}/#/auth/callback"
    if error or not code or not state:
        return RedirectResponse(_append(dest, "error", error or "login_failed"))
    try:
        payload = read_state(state)
        if payload.get("provider") != provider:
            raise AuthError("state/provider mismatch")
        if _valid_ext_redirect(payload.get("redirect")):
            dest = payload["redirect"]        # send the token back to the extension
        profile = oauth.exchange(provider, code)
        user = users.upsert_from_profile(profile)
        token = _session_for(user)
    except (AuthError, OAuthError) as err:
        return RedirectResponse(_append(dest, "error", type(err).__name__))
    return RedirectResponse(_append(dest, "token", token))


@app.get("/api/auth/me")
def auth_me(user: User = Depends(get_current_user)):
    return _user_public(user)


# --------------------------------------------------------------------- config

@app.get("/api/config")
def get_config():
    return {"ratings": RATINGS, "retention": settings.fsrs_retention, "algorithm": "FSRS"}


@app.get("/api/index")
def get_index(user: User = Depends(get_current_user)):
    """Theme/subtheme tree with counts, for the training filters."""
    pool = cards.all(user.id)
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
def random_question(req: RandomRequest, user: User = Depends(get_current_user)):
    pool = [c for c in cards.all(user.id) if _matches(c, req)]
    if not pool:
        return JSONResponse(status_code=404, content={"detail": "No cards match the filters."})

    now_epoch = time.time()
    now_dt = datetime.now(timezone.utc)
    schedules = store.schedules(user.id)
    card = _pick(pool, schedules, set(req.exclude), req.mode, now_epoch)

    due_count = sum(1 for c in pool if c.id in schedules and schedules[c.id]["due"] <= now_epoch)
    new_count = sum(1 for c in pool if c.id not in schedules)
    return {
        "question": _serialise(card, now_dt, user.id),
        "pool_size": len(pool),
        "due_count": due_count,
        "new_count": new_count,
    }


@app.post("/api/reviews")
def record_review(req: ReviewRequest, user: User = Depends(get_current_user)):
    card = cards.get(user.id, req.question_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    progress = store.record(user.id, req.question_id, req.rating, meta=_meta(card))
    return {"progress": progress}


@app.get("/api/stats")
def get_stats(user: User = Depends(get_current_user)):
    live_ids = {c.id for c in cards.all(user.id)}
    now = datetime.now(timezone.utc)
    rows = store.stats(user.id, now)
    for row in rows:
        row["orphaned"] = row["question_id"] not in live_ids
    return {"rows": rows, "now": now.timestamp()}


# --------------------------------------------------------------- card CRUD

@app.get("/api/cards")
def list_cards(
    user: User = Depends(get_current_user),
    theme: str | None = None,
    subtheme: str | None = None,
    search: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    rows = cards.list(user.id, theme=theme, subtheme=subtheme, search=search, limit=limit, offset=offset)
    return {"cards": [_card_summary(c) for c in rows], "total": cards.count(user.id)}


@app.post("/api/cards", status_code=201)
def create_card(body: CardIn, user: User = Depends(get_current_user)):
    limit = settings.daily_card_limit
    if limit > 0 and cards.created_since(user.id, time.time() - 86400) >= limit:
        raise HTTPException(
            status_code=429,
            detail=f"Daily limit reached: at most {limit} new cards per 24 hours.",
        )
    return _card_full(cards.create(user.id, body.model_dump()))


@app.get("/api/cards/{card_id}")
def get_card(card_id: str, user: User = Depends(get_current_user)):
    card = cards.get(user.id, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return _card_full(card)


@app.get("/api/cards/{card_id}/study")
def study_card(card_id: str, user: User = Depends(get_current_user)):
    """The study-screen view of one card — used to refresh in place after an inline edit."""
    card = cards.get(user.id, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return _serialise(card, datetime.now(timezone.utc), user.id)


@app.put("/api/cards/{card_id}")
def update_card(card_id: str, body: CardIn, user: User = Depends(get_current_user)):
    card = cards.update(user.id, card_id, body.model_dump(exclude_unset=True))
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return _card_full(card)


@app.delete("/api/cards/{card_id}", status_code=204)
def delete_card(card_id: str, user: User = Depends(get_current_user)):
    if not cards.delete(user.id, card_id):
        raise HTTPException(status_code=404, detail="Card not found")
    return Response(status_code=204)


# --------------------------------------------------- incremental reading

def _text_doc(text: str) -> dict:
    """Plain text → a ProseMirror doc, one paragraph per blank-line-separated block."""
    blocks = [b.strip() for b in re.split(r"\n{2,}", (text or "").strip())]
    content = [{"type": "paragraph", "content": [{"type": "text", "text": b}] if b else []}
               for b in blocks] or [{"type": "paragraph"}]
    return {"type": "doc", "content": content}


def _reading_node(item: ReadingItem) -> dict:
    return {"id": item.id, "parent_id": item.parent_id, "kind": item.kind,
            "title": item.title, "source_kind": item.source_kind, "created_at": item.created_at}


def _reading_full(item: ReadingItem) -> dict:
    return {**_reading_node(item), "content": item.content, "updated_at": item.updated_at}


@app.get("/api/reading/tree")
def reading_tree(user: User = Depends(get_current_user)):
    """All of the user's documents + extracts (no content) — client builds the tree."""
    return {"items": [_reading_node(i) for i in reading.tree(user.id)]}


@app.get("/api/reading/items/{item_id}")
def reading_get(item_id: str, user: User = Depends(get_current_user)):
    item = reading.get(user.id, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Not found")
    return _reading_full(item)


@app.post("/api/reading/documents", status_code=201)
def reading_create_document(body: ReadingDocIn, user: User = Depends(get_current_user)):
    if not body.content.strip():
        raise HTTPException(status_code=400, detail="Empty document.")
    item = reading.create_document(user.id, body.title or "Untitled", body.content, "text")
    return _reading_full(item)


@app.post("/api/reading/upload", status_code=201)
async def reading_upload(file: UploadFile = File(...), user: User = Depends(get_current_user)):
    from reading import parse_upload

    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large (max 20 MB).")
    try:
        title, text, source_kind = parse_upload(file.filename or "", data)
    except ReadingError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    item = reading.create_document(user.id, title, text, source_kind)
    return _reading_full(item)


@app.post("/api/reading/items/{item_id}/extract", status_code=201)
def reading_extract(item_id: str, body: ExtractIn, user: User = Depends(get_current_user)):
    if not body.content.strip():
        raise HTTPException(status_code=400, detail="Nothing selected.")
    item = reading.create_extract(user.id, item_id, body.content, body.title)
    if item is None:
        raise HTTPException(status_code=404, detail="Parent not found")
    return _reading_full(item)


@app.patch("/api/reading/items/{item_id}")
def reading_update(item_id: str, body: ReadingUpdate, user: User = Depends(get_current_user)):
    item = reading.update(user.id, item_id, body.model_dump(exclude_unset=True))
    if item is None:
        raise HTTPException(status_code=404, detail="Not found")
    return _reading_full(item)


@app.delete("/api/reading/items/{item_id}", status_code=204)
def reading_delete(item_id: str, user: User = Depends(get_current_user)):
    if not reading.delete(user.id, item_id):
        raise HTTPException(status_code=404, detail="Not found")
    return Response(status_code=204)


@app.post("/api/reading/items/{item_id}/card", status_code=201)
def reading_make_card(item_id: str, body: MakeCardRequest, user: User = Depends(get_current_user)):
    """Create a card from an extract — the answer defaults to the extract's text."""
    item = reading.get(user.id, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Not found")
    limit = settings.daily_card_limit
    if limit > 0 and cards.created_since(user.id, time.time() - 86400) >= limit:
        raise HTTPException(status_code=429,
                            detail=f"Daily limit reached: at most {limit} new cards per 24 hours.")
    answer_text = body.answer if body.answer is not None else item.content
    card = cards.create(user.id, {
        "question": _text_doc(body.question),
        "answer": _text_doc(answer_text),
        "theme": body.theme, "subtheme": body.subtheme, "tags": body.tags,
        "source_extract_id": item.id,
    })
    return _card_full(card)


# ------------------------------------------------------------ optimizer

@app.get("/api/optimizer/status")
def optimizer_status(user: User = Depends(get_current_user)):
    return optimizer.status(user.id)


@app.post("/api/optimizer/run")
def optimizer_run(user: User = Depends(get_current_user)):
    try:
        return optimizer.run(user.id)
    except OptimizerUnavailable as error:
        raise HTTPException(status_code=501, detail=str(error)) from error
    except NotEnoughReviews as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Optimisation failed: {error}") from error


# ------------------------------------------------------------------- admin

@app.post("/api/admin/claim-orphans")
def claim_orphans(user: User = Depends(require_admin)):
    """One-shot: assign all pre-multi-user rows (no owner) to the calling admin."""
    return {"claimed": users.claim_orphans(user.id), "user_id": user.id}


@app.post("/api/admin/import-gitbook")
def import_gitbook(user: User = Depends(require_admin)):
    """Re-import the GitBook source into the calling admin's account, ADD-ONLY.

    Adds questions that aren't already in the account (matched by rendered question text)
    and never modifies an existing card. When a new question matches this user's dangling
    review history (a card that was removed), it reuses that old id so the history
    reconnects. Restores full question + answer from the source.
    """
    try:
        questions = source.load(force=True)
    except SourceError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    src = settings.source_dir
    live = cards.all(user.id)
    existing_by_text = {doc_to_text(c.question).strip().lower(): c for c in live}
    orphan_by_text = store.dangling_history(user.id, {c.id for c in live})

    imported = reconnected = filled = 0
    for question in questions:
        q_doc = html_to_doc(render_inline(question.question, src))
        key = doc_to_text(q_doc).strip().lower()
        if not key:
            continue
        a_doc = html_to_doc(render_answer(question.body, src))
        old_id = orphan_by_text.get(key)
        existing = existing_by_text.get(key)

        if existing is not None:
            # Already have this question: never overwrite, but fill an EMPTY answer from
            # the source and reconnect any dangling history to this card.
            if not _has_answer(existing) and doc_to_text(a_doc).strip():
                cards.update(user.id, existing.id, {"answer": a_doc})
                filled += 1
            if old_id and store.reconnect_history(old_id, existing.id):
                reconnected += 1
            continue

        # Missing question: add it, reusing dangling history's id to reconnect if present.
        created = cards.create(user.id, {
            "id": old_id,
            "question": q_doc,
            "answer": a_doc,
            "theme": question.theme,
            "subtheme": question.subtheme,
            "tags": [question.section] if question.section else [],
        })
        existing_by_text[key] = created
        imported += 1
        if old_id:
            reconnected += 1
    return {"imported": imported, "filled_answers": filled,
            "reconnected": reconnected, "total_cards": cards.count(user.id)}


@app.post("/api/admin/purge-orphaned")
def purge_orphaned(user: User = Depends(require_admin)):
    """Delete the caller's review history that isn't linked to a real card (the
    'removed from source' rows). Cards are untouched — only dangling history is removed."""
    live_ids = {c.id for c in cards.all(user.id)}
    return {"deleted": store.purge_orphaned(user.id, live_ids), "user_id": user.id}


@app.post("/api/admin/restore-orphaned")
def restore_orphaned(user: User = Depends(require_admin)):
    """Rebuild cards for the caller's study history whose card was removed from the source.

    Additive/idempotent — never touches existing cards; reconnects each restored card to
    its old history by reusing the id. Answers can't be recovered (only the question text
    survived), so restored cards have an empty answer.
    """
    return {**cards.restore_orphaned(user.id), "user_id": user.id}


# --------------------------------------------------------------------- assets

@app.get("/asset")
def asset(path: str = Query(..., description="Repository-relative asset path")):
    # Public read-only: <img> tags can't send Authorization headers, and imported
    # images aren't sensitive. Serves assets referenced by already-imported cards.
    try:
        content, content_type = source.fetch_asset(path)
    except SourceError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return Response(
        content=content,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )
