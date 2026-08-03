"""Password hashing for email/password accounts (bcrypt)."""

from __future__ import annotations

import bcrypt

# bcrypt only reads the first 72 bytes; cap explicitly so long inputs don't raise.
_MAX = 72


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8")[:_MAX], bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:_MAX], hashed.encode("utf-8"))
    except ValueError:
        return False
