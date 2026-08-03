"""Auth unit checks (no DB): `python tests/test_auth.py`.

Covers the session/state JWTs and the OAuth authorize-URL builder. Provider network
calls and DB-backed user provisioning are exercised end-to-end against a real
deployment, not here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Configure the environment BEFORE settings are first read.
os.environ.update({
    "JWT_SECRET": "test-secret-please-ignore",
    "PUBLIC_BACKEND_URL": "https://api.example.com",
    "FRONTEND_URL": "https://app.example.com",
    "ADMIN_EMAILS": "boss@example.com",
    "GOOGLE_CLIENT_ID": "gid", "GOOGLE_CLIENT_SECRET": "gsec",
    "GITHUB_CLIENT_ID": "hid", "GITHUB_CLIENT_SECRET": "hsec",
    "YANDEX_CLIENT_ID": "yid", "YANDEX_CLIENT_SECRET": "ysec",
    "DATABASE_URL": "postgresql://u:p@localhost:5432/x",  # never connected to here
})

from gitbook.config import get_settings  # noqa: E402

get_settings.cache_clear()

from auth import oauth  # noqa: E402
from auth.tokens import AuthError, issue_session, issue_state, read_session, read_state  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label} {detail}")


print("session token round-trip")
tok = issue_session(user_id="u1", email="a@b.com", is_admin=True)
claims = read_session(tok)
check("sub/email/admin preserved",
      claims["sub"] == "u1" and claims["email"] == "a@b.com" and claims["is_admin"] is True, str(claims))

print("token type isolation")
state = issue_state("google", "nonce123")
sc = read_state(state)
check("state carries provider+nonce", sc["provider"] == "google" and sc["nonce"] == "nonce123")
try:
    read_session(state); check("session-reader rejects state token", False)
except AuthError:
    check("session-reader rejects state token", True)
try:
    read_state(tok); check("state-reader rejects session token", False)
except AuthError:
    check("state-reader rejects session token", True)

print("tampering / bad secret")
try:
    read_session(tok + "x"); check("tampered token rejected", False)
except AuthError:
    check("tampered token rejected", True)

print("provider discovery + authorize URLs")
check("all three providers configured", oauth.available_providers() == ["google", "github", "yandex"],
      str(oauth.available_providers()))
for provider, host in [("google", "accounts.google.com"), ("github", "github.com"),
                       ("yandex", "oauth.yandex.ru")]:
    url = oauth.authorize_url(provider, "STATE")
    ok = (host in url
          and "state=STATE" in url
          and "api.example.com%2Fapi%2Fauth%2F" in url  # url-encoded redirect_uri
          and f"{provider}%2Fcallback" in url)
    check(f"{provider} authorize url", ok, url)

print("admin email matching")
check("admin flagged", get_settings().is_admin_email("Boss@Example.com") is True)
check("non-admin not flagged", get_settings().is_admin_email("someone@else.com") is False)

print("email canonicalization (Gmail dots/+tag)")
from gitbook.config import canonical_email  # noqa: E402
check("gmail dots removed", canonical_email("e.didar.2001@gmail.com") == "edidar2001@gmail.com")
check("gmail +tag dropped", canonical_email("E.Didar.2001+srl@googlemail.com") == "edidar2001@gmail.com")
check("gmail aliases compare equal",
      canonical_email("e.didar2001@gmail.com") == canonical_email("e.didar.2001@gmail.com"))
check("non-gmail dots kept", canonical_email("a.b@outlook.com") == "a.b@outlook.com")
check("non-gmail +tag still dropped", canonical_email("a.b+x@outlook.com") == "a.b@outlook.com")

print()
if failures:
    print(f"{len(failures)} check(s) failed")
    sys.exit(1)
print("all auth checks passed")
