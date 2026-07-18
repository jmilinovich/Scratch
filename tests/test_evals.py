from datetime import datetime, timedelta, timezone

from whoop_hr.evals import (
    eval_cadence,
    eval_coverage,
    eval_cross_check,
    eval_physiology,
    eval_reproducibility,
    stream_is_usable,
)
from whoop_hr.internal import _hrseries_from_metrics
from whoop_hr.models import HRSample, HRSeries, HRSource, Recovery
from whoop_hr.official import _hrseries_from_stream


def _series(bpms, cadence_s=6):
    start = datetime(2026, 1, 1, 2, 0, tzinfo=timezone.utc)
    samples = [
        HRSample(ts=start + timedelta(seconds=i * cadence_s), bpm=float(b))
        for i, b in enumerate(bpms)
    ]
    s = HRSeries(source=HRSource.OFFICIAL_STREAM, sleep_id="x", samples=samples)
    if samples:
        s.window_start = samples[0].ts
        s.window_end = samples[-1].ts + timedelta(seconds=cadence_s)
    return s


def test_stream_is_usable_rejects_zero_stub():
    ok, why = stream_is_usable(_series([0] * 100))
    assert not ok and "zero" in why.lower()


def test_stream_is_usable_rejects_coarse_cadence():
    ok, why = stream_is_usable(_series([60] * 50, cadence_s=300))
    assert not ok and "cadence" in why.lower()


def test_stream_is_usable_accepts_good_stream():
    ok, _ = stream_is_usable(_series([58 + (i % 10) for i in range(300)], cadence_s=6))
    assert ok


def test_eval_coverage_and_cadence():
    s = _series([60] * 300, cadence_s=6)
    assert eval_coverage(s).passed
    assert eval_cadence(s).passed


def test_eval_physiology_rejects_impossible():
    s = _series([250] * 100, cadence_s=6)  # impossible at rest
    assert not eval_physiology(s).passed


def test_eval_physiology_accepts_realistic_night():
    # dips to a nadir mid-sleep
    bpms = [65 - abs(50 - i) // 5 for i in range(100)]
    assert eval_physiology(_series(bpms)).passed


def test_eval_cross_check_against_official():
    s = _series([60] * 100)
    rec = Recovery(None, "x", None, 50, 55, 40, 96, 33.5)
    # avg 60 vs RHR 55 nadir 60 -> plausible; no cycle avg given
    assert eval_cross_check(s, cycle_avg_hr=62, recovery=rec).passed
    # wildly divergent cycle avg should fail
    assert not eval_cross_check(s, cycle_avg_hr=120, recovery=rec).passed


def test_eval_reproducibility():
    a = _series([60, 61, 62])
    b = _series([60, 61, 62])
    c = _series([60, 61, 99])
    assert eval_reproducibility(a, b).passed
    assert not eval_reproducibility(a, c).passed


def test_parse_official_stream_shape():
    raw = {
        "algorithm_version": "1.0",
        "stream": [
            {"time": "2026-01-01T02:00:00Z", "hr": 60},
            {"time": "2026-01-01T02:00:06Z", "hr": 59},
        ],
    }
    s = _hrseries_from_stream(raw, "sid")
    assert s.count == 2 and s.source is HRSource.OFFICIAL_STREAM
    assert s.average_bpm() == 59.5


def test_parse_internal_metrics_shape():
    # Validated live shape: values:[{data:<bpm>, time:<epoch_ms>}]
    raw = {"name": "heart_rate", "start": "2026-01-01T02:00:00Z", "values": [
        {"data": 61, "time": 1767232800000},
        {"data": 62, "time": 1767232806000},
    ]}
    s = _hrseries_from_metrics(raw, "2026-01-01T02:00:00Z", "2026-01-01T02:01:00Z", "sid")
    assert s.count == 2 and s.source is HRSource.INTERNAL_BFF
    assert s.average_bpm() == 61.5
    assert s.median_gap_seconds() == 6.0
