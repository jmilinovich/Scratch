"""Internal WHOOP web-app ("BFF") API client — PATH #3, the only path that
delivers true 6-second overnight HR.

⚠️  This is the reverse-engineered API that app.whoop.com calls. Undocumented,
against WHOOP's Terms of Service, and can break without notice. Isolated in this
one module on purpose so it is rip-and-replaceable.

Auth (validated 2026-07-18): the web app authenticates the `metrics-service`
with a **bearer token** it stores in the readable `whoop-auth-token` cookie —
NOT the account password (the `sign-in` endpoint is gateway-blocked to non-
browser clients). We therefore authenticate with a token bundle bootstrapped
from a logged-in browser session (see `whoop-hr-bootstrap`) and persisted to a
local JSON file:

    { "access_token", "refresh_token", "expiry", "user_id" }

The token is valid ~24h. Refresh is Cloudflare-gated on app.whoop.com; see
README "Keeping the token fresh" for the durable options.

HR response shape (validated):  {"name","start","values":[{"data":<bpm>,"time":<epoch_ms>}]}
Endpoint: GET /metrics-service/v1/metrics/user/{id}?apiVersion=7&name=heart_rate&order=t&step={6|60|600}&start=&end=
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .config import Config
from .models import HRSample, HRSeries, HRSource, parse_ts

API_BASE = "https://api.prod.whoop.com"


class Endpoints:
    """Centralized endpoint table — WHOOP moving something = a one-line fix."""

    METRICS = "/metrics-service/v1/metrics/user/{user_id}"
    # Legacy (Cloudflare/gateway-blocked to non-browser clients; kept for reference):
    SIGN_IN = "/auth-service/v2/whoop/sign-in"
    USER = "/auth-service/v2/user"


class InternalAPIError(RuntimeError):
    pass


class TokenExpiredError(InternalAPIError):
    """The bootstrapped bearer token has expired and must be refreshed."""


class InternalClient:
    """Bearer-token client for true 6-second HR from the web-app API."""

    def __init__(self, config: Config, http: httpx.Client | None = None):
        self.cfg = config
        self._http = http or httpx.Client(timeout=30.0)
        self._bundle = self._load_bundle()

    # -- token bundle --------------------------------------------------------

    def _token_path(self) -> Path:
        return Path(self.cfg.internal_token_file)

    def _load_bundle(self) -> dict[str, Any]:
        p = self._token_path()
        if not p.exists():
            raise InternalAPIError(
                f"no internal token file at {p}; run `whoop-hr-bootstrap` to create it"
            )
        return json.loads(p.read_text())

    @property
    def user_id(self) -> str:
        uid = self._bundle.get("user_id")
        if uid is None:
            raise InternalAPIError("token bundle missing user_id")
        return str(uid)

    def expiry(self) -> datetime | None:
        exp = self._bundle.get("expiry")
        if not exp:
            return None
        try:
            # Bootstrapped from JS `new Date(...).toString()`, e.g.
            # "Sun Jul 19 2026 14:57:25 GMT-0700 (Pacific Daylight Time)".
            cleaned = exp.split(" GMT")[0]
            return datetime.strptime(cleaned, "%a %b %d %Y %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except (ValueError, AttributeError):
            return None

    def is_expired(self, skew_s: int = 120) -> bool:
        exp = self.expiry()
        if exp is None:
            return False  # unknown expiry: let the API be the judge
        return datetime.now(timezone.utc).timestamp() >= (exp.timestamp() - skew_s)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._bundle['access_token']}"}

    # -- heart rate ----------------------------------------------------------

    def get_heart_rate(
        self, start: str, end: str, step: int = 6, sleep_id: str | None = None
    ) -> HRSeries:
        """True per-sample HR. `step` in seconds: 6 | 60 | 600."""
        if step not in (6, 60, 600):
            raise ValueError("step must be 6, 60, or 600 seconds")
        if self.is_expired():
            raise TokenExpiredError(
                "internal bearer token expired; re-run `whoop-hr-bootstrap`"
            )
        url = API_BASE + Endpoints.METRICS.format(user_id=self.user_id)
        params = {
            "apiVersion": 7,
            "name": "heart_rate",
            "order": "t",
            "step": step,
            "start": start,
            "end": end,
        }
        r = self._http.get(url, params=params, headers=self._headers())
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", "2")))
            r = self._http.get(url, params=params, headers=self._headers())
        if r.status_code == 401:
            raise TokenExpiredError(
                "internal API returned 401; token invalid — re-run `whoop-hr-bootstrap`"
            )
        if r.status_code >= 400:
            raise InternalAPIError(f"metrics failed: HTTP {r.status_code}")
        return _hrseries_from_metrics(r.json(), start, end, sleep_id)


def _hrseries_from_metrics(
    raw: dict, start: str, end: str, sleep_id: str | None
) -> HRSeries:
    """Parse the metrics-service response into an HRSeries.

    Validated shape: {"name","start","values":[{"data":<bpm>,"time":<epoch_ms>}]}.
    Tolerant of older key spellings just in case.
    """
    values = None
    if isinstance(raw, dict):
        values = raw.get("values") or raw.get("heart_rate") or raw.get("data")
    values = values or []

    samples: list[HRSample] = []
    for e in values:
        if isinstance(e, dict):
            ts_raw = e.get("time") or e.get("timestamp") or e.get("ts") or e.get("t")
            hr_raw = e.get("data")
            if hr_raw is None:
                hr_raw = e.get("value")
            if hr_raw is None:
                hr_raw = e.get("heart_rate")
        elif isinstance(e, (list, tuple)) and len(e) >= 2:
            ts_raw, hr_raw = e[0], e[1]
        else:
            continue
        if ts_raw is None or hr_raw is None:
            continue
        try:
            samples.append(HRSample(ts=parse_ts(ts_raw), bpm=float(hr_raw)))
        except (TypeError, ValueError):
            continue

    series = HRSeries(source=HRSource.INTERNAL_BFF, sleep_id=sleep_id, samples=samples)
    try:
        series.window_start = parse_ts(start)
        series.window_end = parse_ts(end)
    except (TypeError, ValueError):
        if samples:
            series.window_start = min(s.ts for s in samples)
            series.window_end = max(s.ts for s in samples)
    return series.sorted()
