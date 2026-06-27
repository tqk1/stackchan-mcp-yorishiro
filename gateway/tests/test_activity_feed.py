"""Tests for the ``/control/activity`` feed merge helpers (yorishiro)."""

import os

from stackchan_mcp import activity_log
from stackchan_mcp.http_server import (
    _gather_activity,
    _list_presence_reports,
    _parse_limit,
)


# --- _parse_limit -----------------------------------------------------------


def test_parse_limit_default_clamp_malformed():
    assert _parse_limit(None) == 80
    assert _parse_limit("5") == 5
    assert _parse_limit("9999") == 200      # clamped to hi
    assert _parse_limit("0") == 1           # clamped to lo
    assert _parse_limit("abc") == 80        # malformed -> default


# --- _list_presence_reports -------------------------------------------------


def test_list_presence_reports(tmp_path):
    (tmp_path / "2026-06-25.json").write_text("{}", "utf-8")
    (tmp_path / "2026-06-26.json").write_text("{}", "utf-8")
    (tmp_path / "2026-06-26.md").write_text("# md ignored", "utf-8")
    items = _list_presence_reports(10, path=tmp_path)
    assert {it["subtype"] for it in items} == {"2026-06-25", "2026-06-26"}
    assert all(it["source"] == "report" and it["kind"] == "daily_report" for it in items)
    assert _list_presence_reports(10, path=tmp_path / "missing") == []


# --- _gather_activity (merge of autonomous JSONL + daily reports) -----------


def _setup_sources(monkeypatch, tmp_path):
    jsonl = tmp_path / "activity.jsonl"
    reports = tmp_path / "reports"
    reports.mkdir()
    monkeypatch.setenv("STACKCHAN_ACTIVITY_LOG", str(jsonl))
    monkeypatch.setenv("STACKCHAN_PRESENCE_REPORT", str(reports))
    return jsonl, reports


def test_gather_merges_jsonl_and_reports_newest_first(monkeypatch, tmp_path):
    jsonl, reports = _setup_sources(monkeypatch, tmp_path)
    activity_log.append("heartbeat", "speak", text="a", ts_unix=100.0)
    activity_log.append("home", "command", text="turnOn", ts_unix=300.0)
    rep = reports / "2026-06-26.json"
    rep.write_text("{}", "utf-8")
    os.utime(rep, (200.0, 200.0))

    items = _gather_activity(80, None)
    sources = [it["source"] for it in items]
    assert set(sources) == {"heartbeat", "home", "report"}
    # Merged feed is sorted newest-first by ts_unix across all sources.
    ts = [it["ts_unix"] for it in items]
    assert ts == sorted(ts, reverse=True)


def test_gather_limit_caps_total(monkeypatch, tmp_path):
    _setup_sources(monkeypatch, tmp_path)
    for i in range(10):
        activity_log.append("presence", "transition", ts_unix=float(i))
    assert len(_gather_activity(4, None)) == 4


def test_gather_source_filter_excludes_reports(monkeypatch, tmp_path):
    jsonl, reports = _setup_sources(monkeypatch, tmp_path)
    activity_log.append("home", "command", ts_unix=10.0)
    (reports / "2026-06-26.json").write_text("{}", "utf-8")

    items = _gather_activity(80, "home")
    assert items and all(it["source"] == "home" for it in items)
