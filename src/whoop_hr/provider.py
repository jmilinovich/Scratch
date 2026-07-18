"""Unified interface + runtime selector.

`get_recovery` / `get_sleep_summary` always use the official (consented) API.
`get_sleep_hr` prefers the official sleep stream (path #2) and falls back to
the internal BFF API (path #3) only when the stream is unavailable/empty AND
the internal path is explicitly enabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from .config import Config
from .evals import stream_is_usable
from .internal import InternalClient
from .models import HRSeries, Recovery, SleepSummary
from .official import OfficialAPIError, OfficialClient


@dataclass
class HRResult:
    series: HRSeries | None
    used_fallback: bool
    note: str


def _day_bounds(date_str: str) -> tuple[str, str]:
    """A generous window around a calendar date to capture the overnight sleep.

    Sleep for night-of `date` typically starts the prior evening; we widen to
    [date-1 18:00Z, date+1 12:00Z] and let the sleep record narrow it.
    """
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start = datetime.combine((d - timedelta(days=1)).date(), time(12, 0), tzinfo=timezone.utc)
    end = datetime.combine((d + timedelta(days=1)).date(), time(12, 0), tzinfo=timezone.utc)
    return _iso(start), _iso(end)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pick_sleep_for_date(records: list[dict], date_str: str) -> dict | None:
    """Pick the main (non-nap) sleep whose end lands on the target date."""
    target = datetime.strptime(date_str, "%Y-%m-%d").date()
    best = None
    for r in records:
        if r.get("nap") is True:
            continue
        end = r.get("end")
        if not end:
            continue
        try:
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except ValueError:
            continue
        if end_dt.date() == target:
            # Prefer the longest sleep ending that day.
            dur = (end_dt - datetime.fromisoformat(r["start"].replace("Z", "+00:00")))
            if best is None or dur > best[0]:
                best = (dur, r)
    if best:
        return best[1]
    # Fall back to the most recent sleep in the window.
    return records[-1] if records else None


class WhoopHR:
    def __init__(self, config: Config | None = None):
        self.cfg = config or Config.load()
        self._official: OfficialClient | None = None
        self._internal: InternalClient | None = None

    @property
    def official(self) -> OfficialClient:
        if self._official is None:
            self._official = OfficialClient(self.cfg)
        return self._official

    @property
    def internal(self) -> InternalClient | None:
        if not self.cfg.has_internal:
            return None
        if self._internal is None:
            self._internal = InternalClient(self.cfg)
        return self._internal

    # -- always official -----------------------------------------------------

    def get_recovery(self, start: str, end: str) -> list[Recovery]:
        return self.official.recovery_collection(start, end)

    def get_sleep_summary(self, start: str, end: str) -> list[SleepSummary]:
        return self.official.sleep_summaries(start, end)

    # -- the selector --------------------------------------------------------

    def get_sleep_hr(self, date: str) -> HRResult:
        """Intraday HR for the night ending on `date` (YYYY-MM-DD)."""
        win_start, win_end = _day_bounds(date)
        records = self.official.sleep_collection(win_start, win_end)
        sleep = _pick_sleep_for_date(records, date)
        if not sleep:
            return HRResult(None, False, f"no sleep record found for {date}")
        sleep_id = sleep.get("id")
        s_start = sleep.get("start", win_start)
        s_end = sleep.get("end", win_end)

        # PATH #2 first — the consented, stable option.
        try:
            series = self.official.sleep_hr_stream(sleep_id)
            series.window_start = series.window_start or _parse(s_start)
            series.window_end = series.window_end or _parse(s_end)
            ok, why = stream_is_usable(series)
            if ok:
                return HRResult(series, False, f"official sleep stream: {why}")
            stream_note = f"official stream unusable ({why})"
        except OfficialAPIError as e:
            stream_note = f"official stream error (HTTP {e.status})"

        # PATH #3 fallback — only if enabled.
        if self.internal is None:
            return HRResult(
                None,
                False,
                f"{stream_note}; internal fallback disabled "
                f"(set WHOOP_ALLOW_INTERNAL=true + credentials to enable)",
            )
        series = self.internal.get_heart_rate(s_start, s_end, step=6, sleep_id=sleep_id)
        return HRResult(series, True, f"{stream_note}; used internal BFF 6s HR")


def _parse(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
