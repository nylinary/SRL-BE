"""Signed tokens: the session JWT we issue, and the short-lived OAuth `state`.

Both are HMAC-signed with ``JWT_SECRET``; the state token also carries a CSRF nonce
so a login started in one tab can't be completed by a forged callback.
"""

from __future__ import annotations

import time

import jwt

from gitbook.config import get_settings


class AuthError(Exception):
    """Any token that is missing, malformed, expired, or wrong-purpose."""


def _secret() -> str:
    secret = get_settings().jwt_secret
    if not secret:
        raise AuthError("JWT_SECRET is not configured on the server.")
    return secret


# --------------------------------------------------------------- session token

def issue_session(*, user_id: str, email: str | None, is_admin: bool) -> str:
    settings = get_settings()
    now = int(time.time())
    payload = {
        "sub": user_id,
        "email": email,
        "is_admin": is_admin,
        "iat": now,
        "exp": now + settings.jwt_ttl_days * 86400,
        "typ": "session",
    }
    return jwt.encode(payload, _secret(), algorithm="HS256")


def read_session(token: str) -> dict:
    try:
        payload = jwt.decode(token, _secret(), algorithms=["HS256"])
    except jwt.PyJWTError as error:
        raise AuthError(str(error)) from error
    if payload.get("typ") != "session":
        raise AuthError("Not a session token.")
    return payload


# ------------------------------------------------------------------ oauth state

def issue_state(provider: str, nonce: str, redirect: str | None = None) -> str:
    now = int(time.time())
    payload = {"provider": provider, "nonce": nonce, "redirect": redirect,
               "iat": now, "exp": now + 600, "typ": "state"}
    return jwt.encode(payload, _secret(), algorithm="HS256")


def read_state(token: str) -> dict:
    try:
        payload = jwt.decode(token, _secret(), algorithms=["HS256"])
    except jwt.PyJWTError as error:
        raise AuthError(str(error)) from error
    if payload.get("typ") != "state":
        raise AuthError("Not a state token.")
    return payload
