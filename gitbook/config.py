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

    @property
    def has_token(self) -> bool:
        return bool(self.token)

    @property
    def source_dir(self) -> str:
        """Directory of the markdown file inside the repo — assets resolve against it."""
        return os.path.dirname(self.file_path)

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(BASE_DIR / ".env")
        return cls(
            gitlab_url=os.environ.get("GITLAB_URL", "https://gitlab.com").rstrip("/"),
            project=os.environ.get("GITLAB_PROJECT", "nylinary/gitbook-backup"),
            ref=os.environ.get("GITLAB_REF", "main"),
            file_path=os.environ.get("GITBOOK_FILE", "it-database/questions.md"),
            token=os.environ.get("GITLAB_TOKEN", "").strip(),
            cache_ttl=int(os.environ.get("CACHE_TTL", "300")),
            cache_dir=Path(os.environ.get("CACHE_DIR", str(BASE_DIR / ".cache"))),
            request_timeout=float(os.environ.get("REQUEST_TIMEOUT", "15")),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
