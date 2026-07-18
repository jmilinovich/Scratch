"""Official WHOOP developer API client (path #1) + the undocumented sleep
stream (path #2).

OAuth2 authorization-code with automatic refresh. The token is persisted to a
local JSON file so refresh survives process restarts. Endpoint knowledge here
mirrors `hedgertronic/whoop` (the freshest, best-engineered official wrapper),
re-implemented on httpx to keep a single coherent interface across all paths.

The sleep-stream endpoint is NOT in the public Redoc reference. It is the
primary hypothesis to validate (see scripts/validate.py). If your app is not
granted access it will 403/404 — that is a data point, not a bug.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

from .config import DEFAULT_SCOPES, Config
from .models import (
    HRSample,
    HRSeries,
    HRSource,
    Recovery,
    SleepSummary,
    parse_ts,
)

AUTH_URL = "/oauth/oauth2/auth"
TOKEN_URL = "/oauth/oauth2/token"
API_PREFIX = "/developer/v2"

# Sample types accepted by the sleep stream. `hr` is the one that matters.
STREAM_TYPES = [
    "hr",
    "skin_temp",
    "board_temp",
    "battery_temp",
    "charging_status",
    "sleep_classification",
]


class OfficialAPIError(RuntimeError):
    def __init__(self, status: int, url: str, body: str):
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"{status} on {url}: {body[:300]}")


class OfficialClient:
    """Consented, stable, summary-first API — plus the sleep-stream probe."""

    def __init__(self, config: Config, http: httpx.Client | None = None):
        if not config.has_official:
            raise ValueError("Official client requires WHOOP_CLIENT_ID/SECRET")
        self.cfg = config
        self._http = http or httpx.Client(timeout=30.0)
        self._token: dict[str, Any] | None = None
        self._load_token()

    # -- token persistence ---------------------------------------------------

    def _token_path(self) -> Path:
        return Path(self.cfg.token_file)

    def _load_token(self) -> None:
        p = self._token_path()
        if p.exists():
            self._token = json.loads(p.read_text())

    def _save_token(self, token: dict[str, Any]) -> None:
        # Stamp an absolute expiry so refresh decisions don't depend on when
        # the token was fetched.
        if "expires_at" not in token and "expires_in" in token:
            token["expires_at"] = time.time() + float(token["expires_in"])
        self._token = token
        p = self._token_path()
        p.write_text(json.dumps(token, indent=2))
        try:
            p.chmod(0o600)
        except OSError:
            pass

    @property
    def has_token(self) -> bool:
        return bool(self._token and self._token.get("access_token"))

    # -- OAuth authorization-code flow ---------------------------------------

    def authorization_url(self, state: str, scopes: list[str] | None = None) -> str:
        params = {
            "response_type": "code",
            "client_id": self.cfg.client_id,
            "redirect_uri": self.cfg.redirect_uri,
            "scope": " ".join(scopes or DEFAULT_SCOPES),
            "state": state,
        }
        return f"{self.cfg.api_base}{AUTH_URL}?{urllib.parse.urlencode(params)}"

    def exchange_code(self, code: str) -> dict[str, Any]:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.cfg.redirect_uri,
            "client_id": self.cfg.client_id,
            "client_secret": self.cfg.client_secret,
        }
        resp = self._http.post(f"{self.cfg.api_base}{TOKEN_URL}", data=data)
        if resp.status_code >= 400:
            raise OfficialAPIError(resp.status_code, TOKEN_URL, resp.text)
        token = resp.json()
        self._save_token(token)
        return token

    def _refresh(self) -> None:
        if not self._token or "refresh_token" not in self._token:
            raise OfficialAPIError(401, TOKEN_URL, "no refresh_token; re-auth needed")
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self._token["refresh_token"],
            "client_id": self.cfg.client_id,
            "client_secret": self.cfg.client_secret,
            # WHOOP requires the scope on refresh to keep offline access.
            "scope": "offline",
        }
        resp = self._http.post(f"{self.cfg.api_base}{TOKEN_URL}", data=data)
        if resp.status_code >= 400:
            raise OfficialAPIError(resp.status_code, TOKEN_URL, resp.text)
        token = resp.json()
        # WHOOP may not re-send the refresh token on rotation; keep the old one.
        if "refresh_token" not in token and self._token.get("refresh_token"):
            token["refresh_token"] = self._token["refresh_token"]
        self._save_token(token)

    def _access_token(self) -> str:
        if not self._token:
            raise OfficialAPIError(401, TOKEN_URL, "no token; run the auth flow")
        exp = self._token.get("expires_at")
        if exp is not None and time.time() >= (exp - 60):  # refresh 60s early
            self._refresh()
        return self._token["access_token"]

    # -- request plumbing ----------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        url = f"{self.cfg.api_base}{path}"
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        resp = self._http.get(url, params=params, headers=headers)
        if resp.status_code == 401:
            # Token might have been revoked/expired despite our clock; try once.
            self._refresh()
            headers = {"Authorization": f"Bearer {self._access_token()}"}
            resp = self._http.get(url, params=params, headers=headers)
        if resp.status_code == 429:
            retry = float(resp.headers.get("Retry-After", "2"))
            time.sleep(min(retry, 30))
            resp = self._http.get(url, params=params, headers=headers)
        return resp

    def _paginate(self, path: str, start: str, end: str, limit: int = 25) -> list[dict]:
        out: list[dict] = []
        next_token: str | None = None
        while True:
            params: dict[str, Any] = {"start": start, "end": end, "limit": limit}
            if next_token:
                params["nextToken"] = next_token
            resp = self._get(path, params)
            if resp.status_code >= 400:
                raise OfficialAPIError(resp.status_code, path, resp.text)
            payload = resp.json()
            out.extend(payload.get("records", []))
            next_token = payload.get("next_token") or payload.get("nextToken")
            if not next_token:
                break
        return out

    # -- public API ----------------------------------------------------------

    def profile(self) -> dict:
        resp = self._get(f"{API_PREFIX}/user/profile/basic")
        if resp.status_code >= 400:
            raise OfficialAPIError(resp.status_code, "profile", resp.text)
        return resp.json()

    def sleep_collection(self, start: str, end: str) -> list[dict]:
        return self._paginate(f"{API_PREFIX}/activity/sleep", start, end)

    def cycle_collection(self, start: str, end: str) -> list[dict]:
        return self._paginate(f"{API_PREFIX}/cycle", start, end)

    def recovery_collection(self, start: str, end: str) -> list[Recovery]:
        records = self._paginate(f"{API_PREFIX}/recovery", start, end)
        return [_recovery_from(r) for r in records]

    def sleep_summaries(self, start: str, end: str) -> list[SleepSummary]:
        return [_sleep_summary_from(r) for r in self.sleep_collection(start, end)]

    def get_sleep_stream(
        self, sleep_id: str, types: list[str] | None = None
    ) -> dict:
        """PATH #2 — the key test. Undocumented per-timestamp stream.

        Returns the raw JSON. Raises OfficialAPIError on non-2xx so the caller
        (validate.py) can record the exact failure mode (403 partner-gated,
        404 not-available, etc.).
        """
        types = types or ["hr"]
        params = {"types": ",".join(types)}
        resp = self._get(f"{API_PREFIX}/activity/sleep/{sleep_id}/stream", params)
        if resp.status_code >= 400:
            raise OfficialAPIError(resp.status_code, "sleep_stream", resp.text)
        return resp.json()

    def sleep_hr_stream(self, sleep_id: str) -> HRSeries:
        """Parse the sleep stream into an HRSeries (hr samples only)."""
        raw = self.get_sleep_stream(sleep_id, types=["hr"])
        return _hrseries_from_stream(raw, sleep_id)


# -- parsing helpers ---------------------------------------------------------


def _num(*vals: Any) -> float | None:
    for v in vals:
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _recovery_from(r: dict) -> Recovery:
    score = r.get("score") or {}
    return Recovery(
        cycle_id=str(r.get("cycle_id")) if r.get("cycle_id") is not None else None,
        sleep_id=r.get("sleep_id"),
        date=r.get("created_at"),
        recovery_score=_num(score.get("recovery_score")),
        resting_heart_rate=_num(score.get("resting_heart_rate")),
        hrv_rmssd_milli=_num(score.get("hrv_rmssd_milli")),
        spo2_percentage=_num(score.get("spo2_percentage")),
        skin_temp_celsius=_num(score.get("skin_temp_celsius")),
    )


def _sleep_summary_from(r: dict) -> SleepSummary:
    score = r.get("score") or {}
    stage = score.get("stage_summary") or {}
    return SleepSummary(
        sleep_id=r.get("id"),
        start=parse_ts(r["start"]) if r.get("start") else None,
        end=parse_ts(r["end"]) if r.get("end") else None,
        respiratory_rate=_num(score.get("respiratory_rate")),
        sleep_performance_percentage=_num(score.get("sleep_performance_percentage")),
        sleep_efficiency_percentage=_num(score.get("sleep_efficiency_percentage")),
        disturbance_count=(
            int(stage["disturbance_count"])
            if stage.get("disturbance_count") is not None
            else None
        ),
        total_in_bed_ms=stage.get("total_in_bed_time_milli"),
        total_awake_ms=stage.get("total_awake_time_milli"),
        total_light_ms=stage.get("total_light_sleep_time_milli"),
        total_slow_wave_ms=stage.get("total_slow_wave_sleep_time_milli"),
        total_rem_ms=stage.get("total_rem_sleep_time_milli"),
    )


def _hrseries_from_stream(raw: dict, sleep_id: str) -> HRSeries:
    """Tolerant parser: the exact stream shape is undocumented, so accept the
    plausible variants (list under `stream`/`data`, `hr`/`heart_rate` keys,
    `time`/`timestamp`/`ts` keys)."""
    entries = raw.get("stream") or raw.get("data") or raw.get("samples") or []
    samples: list[HRSample] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        ts_raw = e.get("time") or e.get("timestamp") or e.get("ts") or e.get("t")
        hr_raw = e.get("hr")
        if hr_raw is None:
            hr_raw = e.get("heart_rate")
        if ts_raw is None or hr_raw is None:
            continue
        try:
            samples.append(HRSample(ts=parse_ts(ts_raw), bpm=float(hr_raw)))
        except (TypeError, ValueError):
            continue
    series = HRSeries(source=HRSource.OFFICIAL_STREAM, sleep_id=sleep_id, samples=samples)
    if samples:
        series.window_start = min(s.ts for s in samples)
        series.window_end = max(s.ts for s in samples)
    return series.sorted()
