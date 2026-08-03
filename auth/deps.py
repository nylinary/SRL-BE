"""FastAPI dependencies: resolve the Bearer JWT to a User; gate admin-only routes.

The UserRepository is injected once at startup via `bind_users`, keeping these
dependencies import-safe (no DB connection at module load).
"""

from __future__ import annotations

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from gitbook.models import User

from .tokens import AuthError, read_session
from .users import UserRepository

_bearer = HTTPBearer(auto_error=False)
_users: UserRepository | None = None


def bind_users(repo: UserRepository) -> None:
    global _users
    _users = repo


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> User:
    if creds is None or not creds.credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = read_session(creds.credentials)
    except AuthError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    assert _users is not None, "auth.deps.bind_users was not called"
    user = _users.get(payload["sub"])
    if user is None:
        raise HTTPException(status_code=401, detail="User no longer exists")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    return user
