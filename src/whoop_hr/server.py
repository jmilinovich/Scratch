"""MCP server exposing WHOOP HR + recovery + sleep summaries.

Tools:
  - get_sleep_hr(date)              intraday overnight HR for one night
  - get_recovery(start, end)        recovery score / RHR / HRV / SpO2 / skin temp
  - get_sleep_summary(start, end)   stages, respiratory rate, disturbances

Run:  whoop-hr-mcp     (stdio transport, for Claude Desktop / Code)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

from .models import HRSeries
from .provider import WhoopHR

mcp = FastMCP("whoop-hr")
_client: WhoopHR | None = None


def client() -> WhoopHR:
    global _client
    if _client is None:
        _client = WhoopHR()
    return _client


def _series_to_dict(s: HRSeries) -> dict[str, Any]:
    return {
        "source": s.source.value,
        "sleep_id": s.sleep_id,
        "sample_count": s.count,
        "median_gap_seconds": s.median_gap_seconds(),
        "average_bpm": s.average_bpm(),
        "min_bpm": s.min_bpm(),
        "max_bpm": s.max_bpm(),
        "window_start": s.window_start.isoformat() if s.window_start else None,
        "window_end": s.window_end.isoformat() if s.window_end else None,
        "coverage_fraction": s.coverage_fraction(),
        "samples": [
            {"ts": smp.ts.isoformat(), "bpm": smp.bpm} for smp in s.samples
        ],
    }


@mcp.tool()
def get_sleep_hr(date: str) -> dict[str, Any]:
    """Return per-sample heart rate for the night ending on `date` (YYYY-MM-DD).

    Prefers the official consented sleep stream; falls back to the internal API
    only if enabled. The `source` field records which path served the data.
    """
    result = client().get_sleep_hr(date)
    out: dict[str, Any] = {
        "date": date,
        "used_fallback": result.used_fallback,
        "note": result.note,
    }
    out["series"] = _series_to_dict(result.series) if result.series else None
    return out


@mcp.tool()
def get_recovery(start: str, end: str) -> dict[str, Any]:
    """Recovery records between ISO datetimes `start` and `end` (official API).

    Includes recovery_score, resting_heart_rate, hrv_rmssd_milli,
    spo2_percentage, skin_temp_celsius.
    """
    recs = client().get_recovery(start, end)
    return {
        "count": len(recs),
        "records": [
            {
                "cycle_id": r.cycle_id,
                "sleep_id": r.sleep_id,
                "date": r.date,
                "recovery_score": r.recovery_score,
                "resting_heart_rate": r.resting_heart_rate,
                "hrv_rmssd_milli": r.hrv_rmssd_milli,
                "spo2_percentage": r.spo2_percentage,
                "skin_temp_celsius": r.skin_temp_celsius,
            }
            for r in recs
        ],
    }


@mcp.tool()
def get_sleep_summary(start: str, end: str) -> dict[str, Any]:
    """Sleep summaries between ISO datetimes `start` and `end` (official API).

    Includes stage durations (ms), respiratory_rate, performance/efficiency %,
    and disturbance count.
    """
    sleeps = client().get_sleep_summary(start, end)
    return {
        "count": len(sleeps),
        "records": [
            {
                "sleep_id": s.sleep_id,
                "start": s.start.isoformat() if s.start else None,
                "end": s.end.isoformat() if s.end else None,
                "respiratory_rate": s.respiratory_rate,
                "sleep_performance_percentage": s.sleep_performance_percentage,
                "sleep_efficiency_percentage": s.sleep_efficiency_percentage,
                "disturbance_count": s.disturbance_count,
                "stages_ms": {
                    "in_bed": s.total_in_bed_ms,
                    "awake": s.total_awake_ms,
                    "light": s.total_light_ms,
                    "slow_wave": s.total_slow_wave_ms,
                    "rem": s.total_rem_ms,
                },
            }
            for s in sleeps
        ],
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
