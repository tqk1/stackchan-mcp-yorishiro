"""Tests for the presence self-diagnostic report (pure aggregation).

All synthetic records, no I/O. ``now`` and ``tz`` are injected so the
basic/hourly assertions are deterministic; ``tz=timezone.utc`` pins the
human-readable strings.
"""

from __future__ import annotations

from datetime import timezone
from typing import Any

from stackchan_mcp import presence_report as pr


def rec(
    ts: float,
    *,
    state: str = "active",
    present: bool = True,
    pres_flag: bool | None = None,
    presence: int | None = None,
    ambient_c: float | None = None,
) -> dict[str, Any]:
    """Minimal presence-log record. ``pres_flag`` defaults to ``present``."""
    d: dict[str, Any] = {
        "ts_unix": ts,
        "state": state,
        "present": present,
        "pres_flag": present if pres_flag is None else pres_flag,
    }
    if presence is not None:
        d["presence"] = presence
    if ambient_c is not None:
        d["ambient_c"] = ambient_c
    return d


# ---- _stats ----------------------------------------------------------


def test_stats_empty() -> None:
    assert pr._stats([]) == {"n": 0}


def test_stats_basic() -> None:
    s = pr._stats([10, 20, 30, 40, 50])
    assert s["n"] == 5
    assert s["min"] == 10
    assert s["max"] == 50
    assert s["median"] == 30
    assert s["mean"] == 30.0
    assert "p99" in s


# ---- empty / single --------------------------------------------------


def test_build_report_empty() -> None:
    out = pr.build_report([], now=100.0)
    assert out["ok"] is True
    assert out["empty"] is True
    assert out["basic"]["samples"] == 0


def test_build_report_single_sample() -> None:
    # One record must not ZeroDivision on gap math.
    out = pr.build_report([rec(1000.0)], now=1010.0)
    assert out["empty"] is False
    assert out["basic"]["samples"] == 1
    assert out["basic"]["duration_h"] == 0.0
    assert out["basic"]["stale_s"] == 10.0


# ---- basic / tz ------------------------------------------------------


def test_basic_fields_tz_injected() -> None:
    rows = [rec(0.0), rec(3600.0)]
    out = pr.build_report(rows, now=3700.0, tz=timezone.utc)
    b = out["basic"]
    assert b["start_jst"] == "1970-01-01 00:00:00"
    assert b["end_jst"] == "1970-01-01 01:00:00"
    assert b["duration_h"] == 1.0
    assert b["stale_s"] == 100.0


def test_records_sorted_internally() -> None:
    # Out-of-order input is sorted by ts_unix before aggregation.
    out = pr.build_report([rec(20.0), rec(0.0), rec(10.0)], now=30.0)
    assert out["basic"]["start_unix"] == 0.0
    assert out["basic"]["end_unix"] == 20.0


def test_non_numeric_ts_dropped() -> None:
    rows = [rec(0.0), {"ts_unix": "bad", "state": "active"}, rec(10.0)]
    out = pr.build_report(rows, now=20.0)
    assert out["basic"]["samples"] == 2


# ---- sampling / outages ----------------------------------------------


def test_sampling_outages() -> None:
    rows = [rec(0.0), rec(5.0), rec(50.0), rec(55.0)]  # one 45s gap > 30
    out = pr.build_report(rows, now=60.0)
    s = out["sampling"]
    assert s["gaps_over_30s"] == 1
    assert len(s["outages"]) == 1
    assert s["outages"][0]["gap_s"] == 45.0
    assert s["max_gap_s"] == 45.0


# ---- states ----------------------------------------------------------


def test_state_dwell_clips_outage() -> None:
    # A 600s gap between two active samples must not book ~0.17h to active;
    # it is clipped to the median gap (here 10s).
    rows = [rec(0.0), rec(10.0), rec(610.0), rec(620.0)]
    out = pr.build_report(rows, now=630.0)
    # 4 samples, three credited dt's are 10,(clip)10,10 + last median 10 = 40s
    assert out["states"]["active"]["hours"] == round(40 / 3600, 2)


def test_state_transitions() -> None:
    rows = [
        rec(0.0, state="active"),
        rec(10.0, state="active"),
        rec(20.0, state="quiet"),
        rec(30.0, state="absent", present=False),
    ]
    out = pr.build_report(rows, now=40.0)
    assert out["states"]["transitions"] == 2


def test_states_canonical_keys_present() -> None:
    out = pr.build_report([rec(0.0), rec(10.0)], now=20.0)
    for key in ("active", "quiet", "absent", "unknown"):
        assert key in out["states"]


# ---- presence separation ---------------------------------------------


def test_separation_good() -> None:
    rows = [rec(float(i), pres_flag=True, presence=1000) for i in range(30)]
    rows += [
        rec(float(100 + i), state="absent", present=False, presence=5)
        for i in range(30)
    ]
    out = pr.build_report(rows, now=200.0)
    assert out["presence_separation"]["separation_note"] == "good"


def test_separation_overlap() -> None:
    rows = [rec(float(i), pres_flag=True, presence=10) for i in range(30)]
    rows += [
        rec(float(100 + i), state="absent", present=False, presence=10)
        for i in range(30)
    ]
    out = pr.build_report(rows, now=200.0)
    assert out["presence_separation"]["separation_note"] == "overlap"


def test_separation_insufficient() -> None:
    rows = [rec(0.0, presence=1000), rec(10.0, present=False, presence=5)]
    out = pr.build_report(rows, now=20.0)
    assert out["presence_separation"]["separation_note"] == "insufficient"


# ---- valleys ---------------------------------------------------------


def test_valley_basic_active() -> None:
    # ACTIVE segment: T@0, F@10,20,30, T@40 -> one 40s valley (last-true start).
    rows = [
        rec(0.0, present=True),
        rec(10.0, present=False),
        rec(20.0, present=False),
        rec(30.0, present=False),
        rec(40.0, present=True),
    ]
    out = pr.build_report(rows, now=50.0)
    va = out["valleys_active"]
    assert va["n"] == 1
    assert va["max_s"] == 40.0
    assert va["present_segments"] == 1


def test_valley_open_at_segment_end() -> None:
    # Run still open at the end measures to the last sample (20-0 = 20s).
    rows = [rec(0.0, present=True), rec(10.0, present=False), rec(20.0, present=False)]
    out = pr.build_report(rows, now=30.0)
    assert out["valleys_active"]["max_s"] == 20.0


def test_valley_outage_excluded() -> None:
    # A 180s gap inside the false run is an outage, so the valley is dropped.
    rows = [
        rec(0.0, present=True),
        rec(10.0, present=False),
        rec(20.0, present=False),
        rec(200.0, present=False),  # gap 180 > 60 -> outage
        rec(210.0, present=True),
    ]
    out = pr.build_report(rows, now=220.0)
    assert out["valleys_active"]["n"] == 0


def test_valley_leading_run_skipped() -> None:
    # Segment that opens already in a false run has no known start -> skipped.
    rows = [rec(0.0, present=False), rec(10.0, present=False), rec(20.0, present=True)]
    out = pr.build_report(rows, now=30.0)
    assert out["valleys_active"]["n"] == 0


def test_quiet_valley_not_in_active() -> None:
    # A QUIET valley counts in `valleys` but not in `valleys_active`.
    rows = [
        rec(0.0, state="quiet", present=True),
        rec(10.0, state="quiet", present=False),
        rec(20.0, state="quiet", present=True),
    ]
    out = pr.build_report(rows, now=30.0)
    assert out["valleys"]["n"] == 1
    assert out["valleys_active"]["n"] == 0


# ---- recommendation --------------------------------------------------


def test_recommendation_formula_resolved_basis() -> None:
    # valley 40s resolves under the 1080 threshold -> basis=[40],
    # ceil30(40*1.25=50)=60, no censoring, current over-provisioned.
    rows = [
        rec(0.0, present=True),
        rec(10.0, present=False),
        rec(20.0, present=False),
        rec(30.0, present=False),
        rec(40.0, present=True),
    ]
    out = pr.build_report(rows, now=50.0, current_absent_after_s=1080)
    rc = out["recommendation"]
    assert rc["recommended_absent_after_s"] == 60
    assert rc["false_absent_risk_at_recommended"] == 0
    assert rc["censored_active_dropouts"] == 0
    assert rc["auto_apply"] is False
    assert "過剰" in rc["rationale"]  # 1080 vs 60 is over-provisioned


def test_recommendation_excludes_censored_from_basis() -> None:
    # Valleys that hit the current ceiling (~120s) are censored and must
    # not drive the recommendation (else it ratchets up). Basis = resolved.
    current = 120
    censored = [110.0, 112.0, 115.0, 118.0, 117.0, 116.0, 113.0, 111.0, 114.0, 119.0]
    resolved = [50.0, 60.0]
    rc = pr._recommendation(resolved + censored, current)
    # basis = resolved [50,60] -> p99=60 -> ceil30(75)=90, NOT ~current*1.25.
    assert rc["recommended_absent_after_s"] == 90
    assert rc["censored_active_dropouts"] == 10
    assert "短すぎ" in rc["rationale"]  # many censored -> threshold too short


def test_recommendation_all_censored_falls_back() -> None:
    # If every valley is censored, fall back to all of them (can't do better).
    rc = pr._recommendation([110.0, 115.0, 118.0], 120)
    assert rc["censored_active_dropouts"] == 3
    assert rc["recommended_absent_after_s"] == 150  # ceil30(118*1.25=147.5)


def test_recommendation_no_current_uses_all() -> None:
    rc = pr._recommendation([100.0, 200.0, 300.0], None)
    assert rc["censored_active_dropouts"] == 0
    assert rc["recommended_absent_after_s"] == 390  # ceil30(300*1.25=375)


def test_recommendation_no_active_valleys() -> None:
    out = pr.build_report([rec(0.0), rec(10.0)], now=20.0, current_absent_after_s=1080)
    rc = out["recommendation"]
    assert rc["recommended_absent_after_s"] is None
    assert rc["censored_active_dropouts"] == 0
    assert rc["auto_apply"] is False


# ---- hourly ----------------------------------------------------------


def test_hourly_buckets_tz() -> None:
    # Two samples one hour apart (UTC) land in two sorted buckets.
    rows = [
        rec(0.0, presence=100, ambient_c=28.0),
        rec(3600.0, presence=200, ambient_c=29.0),
    ]
    out = pr.build_report(rows, now=3700.0, tz=timezone.utc)
    hourly = out["hourly"]
    assert len(hourly) == 2
    assert hourly[0]["hour"] == "01-01 00"
    assert hourly[1]["hour"] == "01-01 01"
    assert hourly[0]["ambient_median_c"] == 28.0


# ---- robustness ------------------------------------------------------


def test_missing_optional_fields_no_crash() -> None:
    # No presence/ambient/asleep keys at all.
    rows = [
        {"ts_unix": 0.0, "state": "active", "present": True, "pres_flag": True},
        {"ts_unix": 10.0, "state": "active", "present": False, "pres_flag": False},
    ]
    out = pr.build_report(rows, now=20.0)
    assert out["ok"] is True
    assert out["hourly"][0]["presence_median"] is None


# ---- markdown --------------------------------------------------------


def test_render_markdown_empty() -> None:
    md = pr.render_markdown(pr.build_report([], now=0.0))
    assert "データがありません" in md


def test_render_markdown_smoke() -> None:
    rows = [
        rec(0.0, present=True, presence=1000),
        rec(10.0, present=False, presence=5),
        rec(20.0, present=True, presence=1000),
    ]
    md = pr.render_markdown(pr.build_report(rows, now=30.0, current_absent_after_s=600))
    assert "在室診断レポート" in md
    assert "推奨 absent_after_s" in md
    assert isinstance(md, str)
