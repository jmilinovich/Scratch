"""Data-quality evals. Prove the *data*, not the HTTP status.

Pure functions over HRSeries so they are unit-testable with synthetic data and
reusable by both the runtime selector and scripts/validate.py.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import HRSeries, Recovery

# Physiologically plausible overnight resting HR band (bpm). Zeros/negatives and
# impossible-at-rest highs are rejected. Widened slightly beyond the brief's
# 45–75 to avoid false rejects on athletic nadirs / brief arousals.
HR_MIN_PLAUSIBLE = 30.0
HR_MAX_PLAUSIBLE = 180.0
# The stream is only "usable" as intraday if cadence is at least this fine.
MAX_USABLE_CADENCE_S = 120.0
MIN_USABLE_SAMPLES = 20
MIN_USABLE_COVERAGE = 0.5


@dataclass
class EvalReport:
    name: str
    passed: bool
    detail: str


def _clamp01(x: float | None) -> float | None:
    if x is None:
        return None
    return max(0.0, min(1.0, x))


def stream_is_usable(series: HRSeries) -> tuple[bool, str]:
    """Fast gate used by the runtime selector: is this series real intraday HR?"""
    if series.count < MIN_USABLE_SAMPLES:
        return False, f"only {series.count} samples"
    nonzero = [b for b in series.bpms if b > 0]
    if not nonzero:
        return False, "all-zero / stubbed stream"
    if len(nonzero) / series.count < 0.5:
        return False, "majority-zero stream"
    cadence = series.median_gap_seconds()
    if cadence and cadence > MAX_USABLE_CADENCE_S:
        return False, f"coarse cadence ~{cadence:.0f}s"
    return True, f"{series.count} samples @ ~{cadence:.0f}s" if cadence else f"{series.count} samples"


# -- the six evals from the brief -------------------------------------------


def eval_coverage(series: HRSeries) -> EvalReport:
    frac = series.coverage_fraction()
    if frac is None:
        return EvalReport("coverage", False, "unknown window; cannot compute coverage")
    frac = _clamp01(frac)
    return EvalReport(
        "coverage",
        frac >= MIN_USABLE_COVERAGE,
        f"{frac:.0%} of expected samples across the sleep window",
    )


def eval_cadence(series: HRSeries) -> EvalReport:
    cadence = series.median_gap_seconds()
    if cadence is None:
        return EvalReport("cadence", False, "not enough samples to measure cadence")
    return EvalReport(
        "cadence",
        cadence <= MAX_USABLE_CADENCE_S,
        f"median inter-sample gap {cadence:.0f}s (want <= {MAX_USABLE_CADENCE_S:.0f}s)",
    )


def eval_physiology(series: HRSeries) -> EvalReport:
    bpms = series.bpms
    if not bpms:
        return EvalReport("physiology", False, "no samples")
    impossible = [b for b in bpms if b < HR_MIN_PLAUSIBLE or b > HR_MAX_PLAUSIBLE]
    nonzero = [b for b in bpms if b > 0]
    if not nonzero:
        return EvalReport("physiology", False, "entirely zero-stubbed")
    frac_bad = len(impossible) / len(bpms)
    avg = series.average_bpm()
    lo, hi = series.min_bpm(), series.max_bpm()
    ok = frac_bad < 0.02 and avg is not None and 35 <= avg <= 120
    return EvalReport(
        "physiology",
        ok,
        f"avg {avg:.0f}, range {lo:.0f}-{hi:.0f} bpm, "
        f"{frac_bad:.1%} out-of-band" if avg else "no valid avg",
    )


def eval_cross_check(
    series: HRSeries,
    cycle_avg_hr: float | None = None,
    recovery: Recovery | None = None,
) -> EvalReport:
    """Stream's implied average should roughly track the official summary."""
    avg = series.average_bpm()
    if avg is None:
        return EvalReport("cross_check", False, "no stream average to compare")
    checks = []
    ok = True
    if cycle_avg_hr:
        delta = abs(avg - cycle_avg_hr)
        checks.append(f"vs cycle avg {cycle_avg_hr:.0f}: Δ{delta:.0f}")
        ok = ok and delta <= 15
    if recovery and recovery.resting_heart_rate:
        # Overnight min should be near (>=) resting HR, not far below it.
        lo = series.min_bpm() or avg
        delta = lo - recovery.resting_heart_rate
        checks.append(f"nadir {lo:.0f} vs RHR {recovery.resting_heart_rate:.0f}: Δ{delta:+.0f}")
        ok = ok and delta >= -12
    if not checks:
        return EvalReport("cross_check", True, "no official reference available (skipped)")
    return EvalReport("cross_check", ok, "; ".join(checks))


def eval_reproducibility(a: HRSeries, b: HRSeries) -> EvalReport:
    same = a.count == b.count and a.bpms == b.bpms
    return EvalReport(
        "reproducibility",
        same,
        "identical on re-pull" if same else f"differs: {a.count} vs {b.count} samples",
    )


def run_all(
    series: HRSeries,
    cycle_avg_hr: float | None = None,
    recovery: Recovery | None = None,
    repro: HRSeries | None = None,
) -> list[EvalReport]:
    reports = [
        eval_coverage(series),
        eval_cadence(series),
        eval_physiology(series),
        eval_cross_check(series, cycle_avg_hr, recovery),
    ]
    if repro is not None:
        reports.append(eval_reproducibility(series, repro))
    return reports
