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

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from auth import oauth
from auth.deps import bind_users, get_current_user, require_admin
from auth.oauth import OAuthError
from auth.tokens import AuthError, issue_session, issue_state, read_state
from auth.users import BadCredentials, EmailTaken, UserRepository
from content import CardRepository, doc_to_text, render_doc
from gitbook import MarkdownSource, SourceError, get_settings
from gitbook.models import Card, User
from gitbook.optimizer import NotEnoughReviews, OptimizerService, OptimizerUnavailable
from gitbook.store import open_store

settings = get_settings()
source = MarkdownSource(settings)            # used only by the /asset image proxy now
store = open_store(settings)
cards = CardRepository(store.engine)
optimizer = OptimizerService(store, settings)
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


@app.get("/api/auth/{provider}/login")
def auth_login(provider: str):
    if provider not in oauth.available_providers():
        raise HTTPException(status_code=404, detail="Provider not available")
    state = issue_state(provider, secrets.token_urlsafe(16))
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
        return RedirectResponse(f"{dest}?error={error or 'login_failed'}")
    try:
        payload = read_state(state)
        if payload.get("provider") != provider:
            raise AuthError("state/provider mismatch")
        profile = oauth.exchange(provider, code)
        user = users.upsert_from_profile(profile)
        token = _session_for(user)
    except (AuthError, OAuthError) as err:
        return RedirectResponse(f"{dest}?error={type(err).__name__}")
    return RedirectResponse(f"{dest}?token={token}")


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
