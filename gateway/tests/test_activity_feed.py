"""Tests for the ``/control/activity`` feed merge helpers (yorishiro)."""

import os

from stackchan_mcp import activity_log, http_server
from stackchan_mcp.http_server import (
    _gather_activity,
    _list_presence_reports,
    _parse_limit,
    _read_cron_runs,
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
    # Neutralize the real /tmp Obsidian cron logs so these merge tests stay
    # deterministic on a live razer-server (where those logs exist).
    monkeypatch.setattr(http_server, "CRON_JOBS", ())
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


# --- _read_cron_runs (deep-night Obsidian housekeeping crons) ----------------


def test_read_cron_runs_mtime_tail_and_status(tmp_path):
    ok_log = tmp_path / "inbox-drain.log"
    ok_log.write_text("line one\n=== inbox-drain done: new=3 ===\n", "utf-8")
    os.utime(ok_log, (500.0, 500.0))
    err_log = tmp_path / "notes-tidy.log"
    err_log.write_text("ran fine\nPermission denied\n", "utf-8")
    os.utime(err_log, (700.0, 700.0))

    jobs = ((str(ok_log), "Inbox 整理"), (str(err_log), "ノート整理"))
    items = _read_cron_runs(jobs)
    by_label = {it["subtype"]: it for it in items}

    assert by_label["Inbox 整理"]["ts_unix"] == 500.0
    assert by_label["Inbox 整理"]["source"] == "cron"
    assert by_label["Inbox 整理"]["kind"] == "run"
    assert by_label["Inbox 整理"]["status"] == "ok"
    assert by_label["Inbox 整理"]["text"] == "=== inbox-drain done: new=3 ==="
    # An error marker in the last line flags the run.
    assert by_label["ノート整理"]["status"] == "error"


def test_read_cron_runs_skips_missing_log(tmp_path):
    present = tmp_path / "build_moc.log"
    present.write_text("written index.md\n", "utf-8")
    jobs = (
        (str(present), "MOC 構築"),
        (str(tmp_path / "never-ran.log"), "週次ダイジェスト"),
    )
    items = _read_cron_runs(jobs)
    assert {it["subtype"] for it in items} == {"MOC 構築"}


def test_gather_includes_cron(monkeypatch, tmp_path):
    _setup_sources(monkeypatch, tmp_path)
    activity_log.append("home", "command", ts_unix=100.0)
    cron_log = tmp_path / "self-reflect.log"
    cron_log.write_text("=== self-reflect done ===\n", "utf-8")
    os.utime(cron_log, (200.0, 200.0))
    monkeypatch.setattr(
        http_server, "CRON_JOBS", ((str(cron_log), "自己ふりかえり"),)
    )

    merged = _gather_activity(80, None)
    assert {it["source"] for it in merged} == {"home", "cron"}
    # The cron filter returns only cron items.
    only_cron = _gather_activity(80, "cron")
    assert only_cron and all(it["source"] == "cron" for it in only_cron)
