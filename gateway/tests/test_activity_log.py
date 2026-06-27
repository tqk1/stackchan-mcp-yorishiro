"""Tests for ``stackchan_mcp.activity_log`` — the dashboard feed JSONL."""

import json

from stackchan_mcp.activity_log import (
    DEFAULT_LOG_PATH,
    DISABLED_VALUE,
    PATH_ENV_VAR,
    RETENTION_ENV_VAR,
    append,
    read_recent,
    resolve_log_path,
    rotate_old_entries,
)


# --- resolve_log_path -------------------------------------------------------


def test_resolve_default_when_unset(monkeypatch):
    monkeypatch.delenv(PATH_ENV_VAR, raising=False)
    assert resolve_log_path() == DEFAULT_LOG_PATH


def test_resolve_honors_override_and_tilde(monkeypatch):
    monkeypatch.setenv(PATH_ENV_VAR, "~/x/activity.jsonl")
    assert resolve_log_path().name == "activity.jsonl"
    assert "~" not in str(resolve_log_path())


def test_resolve_off_disables(monkeypatch):
    monkeypatch.setenv(PATH_ENV_VAR, DISABLED_VALUE.upper())  # case-insensitive
    assert resolve_log_path() is None


def test_resolve_blank_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(PATH_ENV_VAR, "   ")
    assert resolve_log_path() == DEFAULT_LOG_PATH


# --- append -----------------------------------------------------------------


def test_append_writes_line_with_required_and_optional_fields(tmp_path):
    path = tmp_path / "a.jsonl"
    append(
        "heartbeat",
        "speak",
        subtype="weather",
        text="傘いるよ",
        duration_ms=2450,
        path=path,
        ts_unix=1000.0,
    )
    obj = json.loads(path.read_text("utf-8").strip())
    assert obj == {
        "ts_unix": 1000.0,
        "source": "heartbeat",
        "kind": "speak",
        "status": "ok",
        "subtype": "weather",
        "text": "傘いるよ",
        "duration_ms": 2450,
    }


def test_append_omits_none_optionals(tmp_path):
    path = tmp_path / "a.jsonl"
    append("presence", "transition", path=path, ts_unix=5.0)
    obj = json.loads(path.read_text("utf-8").strip())
    assert "text" not in obj and "subtype" not in obj and "detail" not in obj
    assert obj["status"] == "ok"


def test_append_disabled_is_noop(monkeypatch, tmp_path):
    monkeypatch.setenv(PATH_ENV_VAR, DISABLED_VALUE)
    append("home", "command", text="turnOn")  # resolves to None -> no-op
    # Nothing created in the default dir for this test process; assert via read.
    assert read_recent(10) == []


def test_append_swallows_encode_error(tmp_path):
    path = tmp_path / "a.jsonl"
    # A non-serialisable detail must not raise into the caller.
    append("home", "command", detail={"x": object()}, path=path, ts_unix=1.0)
    assert path.read_text("utf-8") == ""  # write failed cleanly, file empty


# --- read_recent ------------------------------------------------------------


def test_read_recent_newest_first_and_limit(tmp_path):
    path = tmp_path / "a.jsonl"
    for i in range(5):
        append("cron", "usage", path=path, ts_unix=float(i))
    rows = read_recent(3, path=path)
    assert [r["ts_unix"] for r in rows] == [4.0, 3.0, 2.0]


def test_read_recent_source_filter(tmp_path):
    path = tmp_path / "a.jsonl"
    append("heartbeat", "speak", path=path, ts_unix=1.0)
    append("home", "command", path=path, ts_unix=2.0)
    rows = read_recent(10, source="home", path=path)
    assert len(rows) == 1 and rows[0]["source"] == "home"


def test_read_recent_skips_malformed_and_missing(tmp_path):
    path = tmp_path / "a.jsonl"
    path.write_text('not json\n{"ts_unix": 9, "source": "x", "kind": "k"}\n{}\n', "utf-8")
    rows = read_recent(10, path=path)
    assert len(rows) == 1 and rows[0]["ts_unix"] == 9
    assert read_recent(10, path=tmp_path / "missing.jsonl") == []


# --- rotate_old_entries -----------------------------------------------------


def test_rotate_prunes_old_keeps_recent(monkeypatch, tmp_path):
    monkeypatch.setenv(RETENTION_ENV_VAR, "14")
    path = tmp_path / "a.jsonl"
    now = 14 * 24 * 3600 + 100.0
    append("cron", "usage", path=path, ts_unix=10.0)          # older than 14d
    append("cron", "usage", path=path, ts_unix=now - 60.0)    # within window
    rotate_old_entries(path=path, now_unix=now)
    rows = read_recent(10, path=path)
    assert len(rows) == 1 and rows[0]["ts_unix"] == now - 60.0


def test_rotate_missing_file_is_noop(tmp_path):
    rotate_old_entries(path=tmp_path / "missing.jsonl", now_unix=1.0)  # no raise


def test_retention_env_bad_value_uses_default(monkeypatch, tmp_path):
    monkeypatch.setenv(RETENTION_ENV_VAR, "not-an-int")
    path = tmp_path / "a.jsonl"
    append("cron", "usage", path=path, ts_unix=1.0)
    # 1.0 is far older than the 14-day default window relative to a big now.
    rotate_old_entries(path=path, now_unix=30 * 24 * 3600.0)
    assert read_recent(10, path=path) == []
