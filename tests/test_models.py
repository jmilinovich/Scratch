from datetime import datetime, timedelta, timezone

from whoop_hr.models import HRSample, HRSeries, HRSource, parse_ts


def _series(bpms, cadence_s=6, start=None):
    start = start or datetime(2026, 1, 1, 2, 0, tzinfo=timezone.utc)
    samples = [
        HRSample(ts=start + timedelta(seconds=i * cadence_s), bpm=float(b))
        for i, b in enumerate(bpms)
    ]
    s = HRSeries(source=HRSource.OFFICIAL_STREAM, sleep_id="x", samples=samples)
    if samples:
        s.window_start = samples[0].ts
        s.window_end = samples[-1].ts + timedelta(seconds=cadence_s)
    return s


def test_parse_ts_variants():
    a = parse_ts("2026-01-01T02:00:00Z")
    b = parse_ts("2026-01-01T02:00:00+00:00")
    c = parse_ts(1767232800)  # epoch seconds
    d = parse_ts(1767232800000)  # epoch millis
    assert a == b == c == d
    assert a.tzinfo is not None


def test_median_gap_and_stats():
    s = _series([60, 62, 58, 61], cadence_s=6)
    assert s.median_gap_seconds() == 6
    assert s.min_bpm() == 58
    assert s.max_bpm() == 62
    assert 58 <= s.average_bpm() <= 62


def test_average_ignores_zero_stubs():
    s = _series([0, 0, 60, 60], cadence_s=6)
    assert s.average_bpm() == 60
    assert s.min_bpm() == 60  # zeros excluded from min


def test_coverage_fraction():
    # 100 samples at 6s over a 600s window => exactly full coverage.
    s = _series([60] * 100, cadence_s=6)
    frac = s.coverage_fraction(cadence_s=6)
    assert 0.95 <= frac <= 1.06
