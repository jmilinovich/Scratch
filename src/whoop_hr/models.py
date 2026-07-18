"""Domain models plus the data-quality metrics used by the eval harness.

Kept dependency-free (stdlib only) so the eval logic is unit-testable without
any network access.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class HRSource(str, Enum):
    """Which access path actually served a heart-rate series."""

    OFFICIAL_STREAM = "official_stream"  # path #2 — undocumented sleep stream
    INTERNAL_BFF = "internal_bff"  # path #3 — reverse-engineered web-app API


@dataclass(frozen=True)
class HRSample:
    """A single heart-rate reading."""

    ts: datetime  # timezone-aware UTC
    bpm: float


@dataclass
class HRSeries:
    """An ordered per-sample heart-rate series for one sleep/night."""

    source: HRSource
    sleep_id: str | None
    samples: list[HRSample] = field(default_factory=list)
    # Advertised sleep window, if known (used for coverage).
    window_start: datetime | None = None
    window_end: datetime | None = None

    def sorted(self) -> "HRSeries":
        self.samples.sort(key=lambda s: s.ts)
        return self

    @property
    def bpms(self) -> list[float]:
        return [s.bpm for s in self.samples]

    @property
    def count(self) -> int:
        return len(self.samples)

    def average_bpm(self) -> float | None:
        vals = [b for b in self.bpms if b > 0]
        return statistics.fmean(vals) if vals else None

    def min_bpm(self) -> float | None:
        vals = [b for b in self.bpms if b > 0]
        return min(vals) if vals else None

    def max_bpm(self) -> float | None:
        return max(self.bpms) if self.bpms else None

    def median_gap_seconds(self) -> float | None:
        """Median inter-sample gap — the observed cadence."""
        if self.count < 2:
            return None
        ts = sorted(s.ts for s in self.samples)
        gaps = [(b - a).total_seconds() for a, b in zip(ts, ts[1:])]
        gaps = [g for g in gaps if g > 0]
        return statistics.median(gaps) if gaps else None

    def coverage_fraction(self, cadence_s: float | None = None) -> float | None:
        """Fraction of expected samples present across the sleep window.

        expected = window_duration / cadence.  Returns None if the window is
        unknown.  Values can slightly exceed 1.0 with jitter; caller clamps.
        """
        if self.window_start is None or self.window_end is None:
            return None
        duration = (self.window_end - self.window_start).total_seconds()
        if duration <= 0:
            return None
        cadence = cadence_s or self.median_gap_seconds()
        if not cadence or cadence <= 0:
            return None
        expected = duration / cadence
        if expected <= 0:
            return None
        return self.count / expected


@dataclass(frozen=True)
class Recovery:
    """One recovery record (official API, always available & useful)."""

    cycle_id: str | None
    sleep_id: str | None
    date: str | None
    recovery_score: float | None
    resting_heart_rate: float | None
    hrv_rmssd_milli: float | None
    spo2_percentage: float | None
    skin_temp_celsius: float | None


@dataclass(frozen=True)
class SleepSummary:
    """One sleep record summary (official API)."""

    sleep_id: str | None
    start: datetime | None
    end: datetime | None
    respiratory_rate: float | None
    sleep_performance_percentage: float | None
    sleep_efficiency_percentage: float | None
    disturbance_count: int | None
    # Stage durations in milliseconds.
    total_in_bed_ms: int | None = None
    total_awake_ms: int | None = None
    total_light_ms: int | None = None
    total_slow_wave_ms: int | None = None
    total_rem_ms: int | None = None


def parse_ts(value: str | int | float) -> datetime:
    """Parse a WHOOP timestamp: ISO-8601 string or epoch seconds/millis."""
    if isinstance(value, (int, float)):
        # Heuristic: >1e12 means milliseconds.
        secs = value / 1000.0 if value > 1e12 else float(value)
        return datetime.fromtimestamp(secs, tz=timezone.utc)
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
