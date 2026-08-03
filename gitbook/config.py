"""Runtime configuration, read from the environment (optionally via a .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    """Minimal .env reader — existing environment variables always win."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def canonical_email(email: str | None) -> str | None:
    """Normalise an email so aliases of one mailbox compare equal.

    - lowercased and trimmed;
    - ``+tag`` suffix dropped (alias on virtually every modern provider);
    - for Gmail (``gmail.com``/``googlemail.com``) dots in the local part are removed and
      the domain is unified — Gmail ignores both, so ``e.didar.2001@gmail.com`` and
      ``e.didar2001@gmail.com`` are the same account.
    Used for admin matching, account uniqueness, and link-by-email.
    """
    if not email:
        return None
    email = email.strip().lower()
    if "@" not in email:
        return email
    local, _, domain = email.partition("@")
    local = local.split("+", 1)[0]
    if domain in ("gmail.com", "googlemail.com"):
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_parameters(raw: str) -> tuple[float, ...] | None:
    """Parse an optional comma-separated list of FSRS weights; None keeps defaults."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return tuple(float(part) for part in raw.split(",") if part.strip())
    except ValueError:
        return None


@dataclass(frozen=True)
class OAuthCreds:
    client_id: str
    client_secret: str

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


# Providers we know how to talk to; each is enabled only when its env creds are set.
OAUTH_PROVIDERS = ("google", "github", "yandex")


def _load_oauth() -> dict[str, OAuthCreds]:
    out: dict[str, OAuthCreds] = {}
    for name in OAUTH_PROVIDERS:
        creds = OAuthCreds(
            client_id=os.environ.get(f"{name.upper()}_CLIENT_ID", "").strip(),
            client_secret=os.environ.get(f"{name.upper()}_CLIENT_SECRET", "").strip(),
        )
        if creds.configured:
            out[name] = creds
    return out


@dataclass(frozen=True)
class Settings:
    gitlab_url: str
    project: str
    ref: str
    file_path: str
    token: str
    cache_ttl: int
    cache_dir: Path
    request_timeout: float
    database_url: str | None
    fsrs_retention: float
    fsrs_max_interval: int
    fsrs_enable_fuzz: bool
    fsrs_parameters: tuple[float, ...] | None
    cors_origins: list[str]
    # --- auth / multi-user ---
    jwt_secret: str
    jwt_ttl_days: int
    admin_emails: frozenset[str]
    public_backend_url: str        # this API's own public origin (for OAuth redirect_uri)
    frontend_url: str              # where to send the browser back after login
    oauth: dict[str, "OAuthCreds"]
    daily_card_limit: int

    @property
    def has_token(self) -> bool:
        return bool(self.token)

    @property
    def source_dir(self) -> str:
        """Directory of the markdown file inside the repo — assets resolve against it."""
        return os.path.dirname(self.file_path)

    def is_admin_email(self, email: str | None) -> bool:
        canon = canonical_email(email)
        return bool(canon) and canon in self.admin_emails

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(BASE_DIR / ".env")
        cache_dir = Path(os.environ.get("CACHE_DIR", str(BASE_DIR / ".cache")))
        admin_emails = frozenset(
            c for c in (canonical_email(e) for e in os.environ.get("ADMIN_EMAILS", "").split(","))
            if c
        )
        return cls(
            gitlab_url=os.environ.get("GITLAB_URL", "https://gitlab.com").rstrip("/"),
            project=os.environ.get("GITLAB_PROJECT", "nylinary/gitbook-backup"),
            ref=os.environ.get("GITLAB_REF", "main"),
            file_path=os.environ.get("GITBOOK_FILE", "it-database/questions.md"),
            token=os.environ.get("GITLAB_TOKEN", "").strip(),
            cache_ttl=int(os.environ.get("CACHE_TTL", "300")),
            cache_dir=cache_dir,
            request_timeout=float(os.environ.get("REQUEST_TIMEOUT", "15")),
            database_url=(os.environ.get("DATABASE_URL", "").strip() or None),
            fsrs_retention=float(os.environ.get("FSRS_RETENTION", "0.9")),
            fsrs_max_interval=int(os.environ.get("FSRS_MAX_INTERVAL", "36500")),
            fsrs_enable_fuzz=_env_bool("FSRS_ENABLE_FUZZ", True),
            fsrs_parameters=_parse_parameters(os.environ.get("FSRS_PARAMETERS", "")),
            cors_origins=[o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()],
            jwt_secret=os.environ.get("JWT_SECRET", "").strip(),
            jwt_ttl_days=int(os.environ.get("JWT_TTL_DAYS", "30")),
            admin_emails=admin_emails,
            public_backend_url=os.environ.get("PUBLIC_BACKEND_URL", "").rstrip("/"),
            frontend_url=os.environ.get("FRONTEND_URL", "").rstrip("/"),
            oauth=_load_oauth(),
            daily_card_limit=int(os.environ.get("DAILY_CARD_LIMIT", "1000")),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
