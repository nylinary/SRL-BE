"""Fetches the markdown (and its assets) from GitLab, with caching and a disk fallback."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from urllib.parse import quote

import requests

from .config import Settings
from .parser import Question, parse_questions

CACHE_FILENAME = "questions.md"


class SourceError(RuntimeError):
    """The markdown could not be obtained from GitLab or from the local cache."""


class MarkdownSource:
    """Single source of truth for the raw markdown and the parsed questions.

    The last successful download is mirrored to ``CACHE_DIR`` so the app keeps
    working when GitLab is unreachable or the token has expired.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._raw: str | None = None
        self._questions: list[Question] = []
        self._fetched_at: float = 0.0
        self._from_cache = False

    # ------------------------------------------------------------------ URLs

    def _file_url(self, path: str) -> str:
        project = quote(self.settings.project, safe="")
        file_path = quote(path, safe="")
        return (
            f"{self.settings.gitlab_url}/api/v4/projects/{project}"
            f"/repository/files/{file_path}/raw?ref={quote(self.settings.ref, safe='')}"
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self.settings.token} if self.settings.has_token else {}

    @property
    def _cache_file(self) -> Path:
        return self.settings.cache_dir / CACHE_FILENAME

    # --------------------------------------------------------------- fetching

    def _download(self) -> str:
        if not self.settings.has_token:
            raise SourceError(
                "GITLAB_TOKEN is not set. Copy .env.example to .env and add a GitLab "
                "personal access token with read_repository scope."
            )

        response = requests.get(
            self._file_url(self.settings.file_path),
            headers=self._headers,
            timeout=self.settings.request_timeout,
        )
        if response.status_code == 401:
            raise SourceError(
                "GitLab rejected the token (401). It has most likely expired — "
                "generate a new one and update GITLAB_TOKEN in .env."
            )
        if response.status_code == 404:
            raise SourceError(
                f"Not found on GitLab: {self.settings.project}@{self.settings.ref}:"
                f"{self.settings.file_path}"
            )
        if not response.ok:
            raise SourceError(f"GitLab returned HTTP {response.status_code}")

        text = response.text
        # GitLab answers 200 with a JSON error body for some token failures.
        if text.lstrip().startswith('{"error"'):
            try:
                detail = json.loads(text).get("error_description") or json.loads(text)["error"]
            except (ValueError, KeyError):
                detail = text[:200]
            raise SourceError(f"GitLab rejected the request: {detail}")
        return text

    def _write_cache(self, text: str) -> None:
        try:
            self.settings.cache_dir.mkdir(parents=True, exist_ok=True)
            self._cache_file.write_text(text, encoding="utf-8")
        except OSError:
            pass  # A read-only FS is not a reason to fail the request.

    def _read_cache(self) -> str | None:
        try:
            return self._cache_file.read_text(encoding="utf-8")
        except OSError:
            return None

    # ------------------------------------------------------------------- API

    def load(self, force: bool = False) -> list[Question]:
        """Return the parsed questions, refreshing from GitLab when the TTL expired."""
        with self._lock:
            fresh_enough = (
                self._raw is not None
                and not force
                and (time.time() - self._fetched_at) < self.settings.cache_ttl
            )
            if fresh_enough:
                return self._questions

            try:
                raw = self._download()
                self._from_cache = False
                self._write_cache(raw)
            except (SourceError, requests.RequestException) as error:
                cached = self._read_cache()
                if cached is None:
                    raise error if isinstance(error, SourceError) else SourceError(str(error))
                raw, self._from_cache = cached, True

            self._raw = raw
            self._questions = parse_questions(raw)
            self._fetched_at = time.time()
            return self._questions

    def fetch_asset(self, path: str) -> tuple[bytes, str]:
        """Download a repository asset (images referenced from the markdown)."""
        response = requests.get(
            self._file_url(path),
            headers=self._headers,
            timeout=self.settings.request_timeout,
        )
        if not response.ok:
            raise SourceError(f"Asset '{path}' unavailable (HTTP {response.status_code})")
        content_type = response.headers.get("Content-Type", "application/octet-stream")
        return response.content, content_type

    @property
    def status(self) -> dict[str, object]:
        return {
            "source": f"{self.settings.project}@{self.settings.ref}:{self.settings.file_path}",
            "fetched_at": self._fetched_at or None,
            "stale": self._from_cache,
            "cached_copy": self._cache_file.exists(),
        }
