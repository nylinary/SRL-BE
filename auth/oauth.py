"""Social login for Google, GitHub, Yandex and VK.

Each provider gets an authorize URL and a code→profile exchange. Profiles are
normalised to a common shape so the rest of the app never sees provider quirks.
Only providers with configured credentials are usable (see Settings.oauth).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from gitbook.config import get_settings

SUPPORTED = ("google", "github", "yandex", "vk")


class OAuthError(Exception):
    """Provider is unconfigured/unknown, or the exchange failed."""


@dataclass(frozen=True)
class Profile:
    provider: str
    subject: str            # provider's stable user id
    email: str | None
    email_verified: bool
    name: str
    avatar: str


def _redirect_uri(provider: str) -> str:
    base = get_settings().public_backend_url
    if not base:
        raise OAuthError("PUBLIC_BACKEND_URL is not configured on the server.")
    return f"{base}/api/auth/{provider}/callback"


def _creds(provider: str):
    creds = get_settings().oauth.get(provider)
    if creds is None:
        raise OAuthError(f"Provider '{provider}' is not configured.")
    return creds


def available_providers() -> list[str]:
    return [p for p in SUPPORTED if p in get_settings().oauth]


# --------------------------------------------------------------- authorize URLs

def authorize_url(provider: str, state: str) -> str:
    if provider not in SUPPORTED:
        raise OAuthError(f"Unknown provider '{provider}'.")
    creds = _creds(provider)
    redirect = _redirect_uri(provider)
    if provider == "google":
        q = {"client_id": creds.client_id, "redirect_uri": redirect, "response_type": "code",
             "scope": "openid email profile", "state": state, "access_type": "online",
             "prompt": "select_account"}
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(q)
    if provider == "github":
        q = {"client_id": creds.client_id, "redirect_uri": redirect,
             "scope": "read:user user:email", "state": state, "allow_signup": "true"}
        return "https://github.com/login/oauth/authorize?" + urlencode(q)
    if provider == "yandex":
        q = {"response_type": "code", "client_id": creds.client_id,
             "redirect_uri": redirect, "state": state}
        return "https://oauth.yandex.ru/authorize?" + urlencode(q)
    if provider == "vk":
        q = {"client_id": creds.client_id, "redirect_uri": redirect, "response_type": "code",
             "scope": "email", "state": state, "v": "5.131", "display": "page"}
        return "https://oauth.vk.com/authorize?" + urlencode(q)
    raise OAuthError(f"Unknown provider '{provider}'.")  # unreachable


# ------------------------------------------------------------ code → profile

def exchange(provider: str, code: str) -> Profile:
    creds = _creds(provider)
    redirect = _redirect_uri(provider)
    timeout = get_settings().request_timeout
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        if provider == "google":
            return _google(client, creds, redirect, code)
        if provider == "github":
            return _github(client, creds, redirect, code)
        if provider == "yandex":
            return _yandex(client, creds, redirect, code)
        if provider == "vk":
            return _vk(client, creds, redirect, code)
    raise OAuthError(f"Unknown provider '{provider}'.")


def _post_json(client: httpx.Client, url: str, data: dict, headers: dict | None = None) -> dict:
    resp = client.post(url, data=data, headers={"Accept": "application/json", **(headers or {})})
    if resp.status_code >= 400:
        raise OAuthError(f"Token exchange failed ({resp.status_code}).")
    return resp.json()


def _google(client, creds, redirect, code) -> Profile:
    tok = _post_json(client, "https://oauth2.googleapis.com/token", {
        "code": code, "client_id": creds.client_id, "client_secret": creds.client_secret,
        "redirect_uri": redirect, "grant_type": "authorization_code"})
    access = tok.get("access_token")
    if not access:
        raise OAuthError("Google returned no access token.")
    info = client.get("https://openidconnect.googleapis.com/v1/userinfo",
                      headers={"Authorization": f"Bearer {access}"}).json()
    return Profile("google", str(info["sub"]), info.get("email"),
                   bool(info.get("email_verified")), info.get("name") or "", info.get("picture") or "")


def _github(client, creds, redirect, code) -> Profile:
    tok = _post_json(client, "https://github.com/login/oauth/access_token", {
        "code": code, "client_id": creds.client_id, "client_secret": creds.client_secret,
        "redirect_uri": redirect})
    access = tok.get("access_token")
    if not access:
        raise OAuthError("GitHub returned no access token.")
    h = {"Authorization": f"Bearer {access}", "Accept": "application/vnd.github+json"}
    user = client.get("https://api.github.com/user", headers=h).json()
    email, verified = user.get("email"), False
    emails = client.get("https://api.github.com/user/emails", headers=h)
    if emails.status_code < 400:
        for e in emails.json():
            if e.get("primary") and e.get("verified"):
                email, verified = e.get("email"), True
                break
    return Profile("github", str(user["id"]), email, verified,
                   user.get("name") or user.get("login") or "", user.get("avatar_url") or "")


def _yandex(client, creds, redirect, code) -> Profile:
    tok = _post_json(client, "https://oauth.yandex.ru/token", {
        "grant_type": "authorization_code", "code": code,
        "client_id": creds.client_id, "client_secret": creds.client_secret})
    access = tok.get("access_token")
    if not access:
        raise OAuthError("Yandex returned no access token.")
    info = client.get("https://login.yandex.ru/info", params={"format": "json"},
                      headers={"Authorization": f"OAuth {access}"}).json()
    email = info.get("default_email") or (info.get("emails") or [None])[0]
    avatar = ""
    if info.get("default_avatar_id") and not info.get("is_avatar_empty"):
        avatar = f"https://avatars.yandex.net/get-yapic/{info['default_avatar_id']}/islands-200"
    name = info.get("real_name") or info.get("display_name") or info.get("login") or ""
    return Profile("yandex", str(info["id"]), email, bool(email), name, avatar)


def _vk(client, creds, redirect, code) -> Profile:
    tok = client.get("https://oauth.vk.com/access_token", params={
        "client_id": creds.client_id, "client_secret": creds.client_secret,
        "redirect_uri": redirect, "code": code}).json()
    access, user_id = tok.get("access_token"), tok.get("user_id")
    if not access or not user_id:
        raise OAuthError("VK returned no access token.")
    email = tok.get("email")  # only present when the user granted the email scope
    resp = client.get("https://api.vk.com/method/users.get", params={
        "user_ids": user_id, "fields": "photo_200", "access_token": access, "v": "5.131"}).json()
    u = (resp.get("response") or [{}])[0]
    name = " ".join(filter(None, [u.get("first_name"), u.get("last_name")])) or f"vk{user_id}"
    return Profile("vk", str(user_id), email, bool(email), name, u.get("photo_200") or "")
