"""Internal WHOOP web-app ("BFF") API client — PATH #3, FALLBACK ONLY.

⚠️  This is the reverse-engineered API that app.whoop.com calls. It uses your
real account password (not OAuth), is undocumented, can break without notice,
and using it violates WHOOP's Terms of Service. It is isolated in this one
module on purpose so it is rip-and-replaceable. It is only reached when the
official sleep stream (path #2) is proven unavailable AND WHOOP_ALLOW_INTERNAL
is explicitly true.

Endpoint knowledge mirrors `jjur/whoop-data` (verified 2025-10). Passwords are
never logged.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .config import Config
from .models import HRSample, HRSeries, HRSource, parse_ts


class Endpoints:
    """Centralized endpoint table — WHOOP moving something = a one-line fix."""

    SIGN_IN = "/auth-service/v2/whoop/sign-in"
    USER = "/auth-service/v2/user"
    METRICS = "/metrics-service/v1/metrics/user/{user_id}"
    CYCLES = "/core-details-bff/v0/cycles/details"
    SLEEP_EVENTS = "/sleep-service/v1/sleep-events"
    VOW_SLEEP = "/vow-service/v1/vows/sleep/1d/cycle/{cycle_id}"
    VOW_RECOVERY = "/vow-service/v1/vows/recovery/1d/cycle/{cycle_id}"
    SPORTS_HISTORY = "/activities-service/v1/sports/history"


class InternalAPIError(RuntimeError):
    pass


class InternalClient:
    """Credential-based client for true 6-second HR."""

    def __init__(self, config: Config, http: httpx.Client | None = None):
        if not (config.username and config.password):
            raise ValueError("Internal client requires WHOOP_USERNAME/PASSWORD")
        self.cfg = config
        self._http = http or httpx.Client(timeout=30.0)
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._user_id: str | None = None

    # -- auth ----------------------------------------------------------------

    def sign_in(self) -> None:
        url = f"{self.cfg.api_base}{Endpoints.SIGN_IN}"
        resp = self._http.post(
            url, json={"username": self.cfg.username, "password": self.cfg.password}
        )
        if resp.status_code >= 400:
            # Never echo the request body — it contains the password.
            raise InternalAPIError(f"sign-in failed: HTTP {resp.status_code}")
        data = resp.json()
        self._access_token = data.get("access_token")
        self._refresh_token = data.get("refresh_token")
        if not self._access_token:
            raise InternalAPIError("sign-in returned no access_token")

    def _headers(self) -> dict[str, str]:
        if not self._access_token:
            self.sign_in()
        return {"Authorization": f"Bearer {self._access_token}"}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        url = f"{self.cfg.api_base}{path}"
        resp = self._http.get(url, params=params, headers=self._headers())
        if resp.status_code == 401:
            # Re-auth once (jjur's retry-on-401 behavior).
            self.sign_in()
            resp = self._http.get(url, params=params, headers=self._headers())
        return resp

    def user_id(self) -> str:
        if self._user_id:
            return self._user_id
        resp = self._get(Endpoints.USER)
        if resp.status_code >= 400:
            raise InternalAPIError(f"user lookup failed: HTTP {resp.status_code}")
        data = resp.json()
        uid = data.get("id") or data.get("user_id") or (data.get("user") or {}).get("id")
        if uid is None:
            raise InternalAPIError("could not resolve user id")
        self._user_id = str(uid)
        return self._user_id

    # -- heart rate ----------------------------------------------------------

    def get_heart_rate(
        self, start: str, end: str, step: int = 6, sleep_id: str | None = None
    ) -> HRSeries:
        """True per-sample HR. `step` in seconds: 6 | 60 | 600."""
        if step not in (6, 60, 600):
            raise ValueError("step must be 6, 60, or 600 seconds")
        uid = self.user_id()
        params = {
            "start": start,
            "end": end,
            "step": step,
            "name": "heart_rate",
            "apiVersion": 7,
        }
        resp = self._get(Endpoints.METRICS.format(user_id=uid), params)
        if resp.status_code == 429:
            time.sleep(float(resp.headers.get("Retry-After", "2")))
            resp = self._get(Endpoints.METRICS.format(user_id=uid), params)
        if resp.status_code >= 400:
            raise InternalAPIError(f"metrics failed: HTTP {resp.status_code}")
        return _hrseries_from_metrics(resp.json(), start, end, sleep_id)


def _hrseries_from_metrics(
    raw: dict, start: str, end: str, sleep_id: str | None
) -> HRSeries:
    """Parse the metrics-service response into an HRSeries.

    The metrics endpoint returns values keyed by metric name; be tolerant of
    the exact envelope shape.
    """
    values = None
    if isinstance(raw, dict):
        if "heart_rate" in raw:
            values = raw["heart_rate"]
        elif "values" in raw:
            values = raw["values"]
        elif "data" in raw:
            values = raw["data"]
        elif "metrics" in raw and isinstance(raw["metrics"], dict):
            values = raw["metrics"].get("heart_rate")
    values = values or []

    samples: list[HRSample] = []
    for e in values:
        if isinstance(e, dict):
            ts_raw = e.get("time") or e.get("timestamp") or e.get("ts") or e.get("t")
            hr_raw = e.get("value")
            if hr_raw is None:
                hr_raw = e.get("heart_rate")
            if hr_raw is None:
                hr_raw = e.get("data")
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
