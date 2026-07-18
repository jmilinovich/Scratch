"""Configuration loaded from environment / a local .env file.

No third-party dependency for .env parsing — we read it ourselves so the only
runtime deps are httpx and mcp.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: str | Path = ".env") -> None:
    """Populate os.environ from a .env file (existing env vars win)."""
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _bool(val: str | None, default: bool = False) -> bool:
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    api_base: str
    # Official API (path #1/#2)
    client_id: str | None
    client_secret: str | None
    redirect_uri: str
    token_file: str
    # Internal API (path #3, fallback)
    username: str | None
    password: str | None
    allow_internal: bool

    @property
    def has_official(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def has_internal(self) -> bool:
        return bool(self.allow_internal and self.username and self.password)

    @classmethod
    def load(cls, dotenv: str | Path = ".env") -> "Config":
        _load_dotenv(dotenv)
        return cls(
            api_base=os.environ.get("WHOOP_API_BASE", "https://api.prod.whoop.com").rstrip("/"),
            client_id=os.environ.get("WHOOP_CLIENT_ID") or None,
            client_secret=os.environ.get("WHOOP_CLIENT_SECRET") or None,
            redirect_uri=os.environ.get("WHOOP_REDIRECT_URI", "http://localhost:8080/callback"),
            token_file=os.environ.get("WHOOP_TOKEN_FILE", ".whoop_token.json"),
            username=os.environ.get("WHOOP_USERNAME") or None,
            password=os.environ.get("WHOOP_PASSWORD") or None,
            allow_internal=_bool(os.environ.get("WHOOP_ALLOW_INTERNAL"), default=False),
        )


# Default OAuth scopes for the official API. `offline` yields a refresh token.
DEFAULT_SCOPES = [
    "offline",
    "read:profile",
    "read:recovery",
    "read:sleep",
    "read:cycles",
    "read:workout",
    "read:body_measurement",
]
