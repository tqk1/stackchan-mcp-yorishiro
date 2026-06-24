"""Tests for the presence state machine + heartbeat occupancy gate.

The poll logic is driven through :meth:`PresenceMonitor._poll_once` with
a fake ``dispatch`` (the same shape sensors.py expects) and patched
``_now`` / ``_monotonic`` clocks, so ticks are deterministic and no
ESP32 / asyncio sleep loop is involved.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from pathlib import Path
from typing import Any

import pytest

from stackchan_mcp import presence, sensors
from stackchan_mcp.heartbeat import HeartbeatRunner
from stackchan_mcp.presence import PresenceMonitor, PresenceState


def make_dispatch(
    reg_map: dict[tuple[int, int], list[int]] | None = None,
    *,
    fail_addr: int | None = None,
):
    """Fake dispatch returning programmable register bytes (see test_sensors)."""
    reg_map = reg_map or {}

    async def dispatch(name: str, args: dict[str, Any]) -> list[Any]:
        addr = args.get("addr")
        if fail_addr is not None and addr == fail_addr:
            return [{"type": "text", "text": json.dumps({"error": "ESP_ERR_TIMEOUT"})}]
        if name == "i2c_write_read":
            reg = args["write_bytes"][0]
            data = reg_map.get((addr, reg), [0] * args["n_bytes"])
            return [{"type": "text", "text": json.dumps({"ok": True, "bytes": data})}]
        return [{"type": "text", "text": json.dumps({"ok": True})}]

    return dispatch


# Register maps for an occupied vs an empty room (WHO_AM_I lets init_tmos pass).
PRESENT_REG = {
    (0x5A, sensors.TMOS_WHO_AM_I): [0xD3],
    (0x5A, sensors.TMOS_FUNC_STATUS): [0x04],  # PRES flag
    (0x5A, sensors.TMOS_TPRESENCE_L): [0x02, 0x03],  # 770
    (0x5A, sensors.TMOS_TMOTION_L): [0x00, 0x00],
    (0x5A, sensors.TMOS_TOBJECT_L): [0x00, 0x00],
    (0x5A, sensors.TMOS_TAMBIENT_L): [0x00, 0x00],
}
ABSENT_REG = {
    (0x5A, sensors.TMOS_WHO_AM_I): [0xD3],
    (0x5A, sensors.TMOS_FUNC_STATUS): [0x00],
    (0x5A, sensors.TMOS_TPRESENCE_L): [0x0A, 0x00],  # 10
    (0x5A, sensors.TMOS_TMOTION_L): [0x00, 0x00],
    (0x5A, sensors.TMOS_TOBJECT_L): [0x00, 0x00],
    (0x5A, sensors.TMOS_TAMBIENT_L): [0x00, 0x00],
}


class FakeESP32:
    def __init__(self, connected: bool = True) -> None:
        self.device_connected = connected
        self.tts_lock = asyncio.Lock()


class FakeGateway:
    def __init__(self, connected: bool = True) -> None:
        self.esp32 = FakeESP32(connected)
        self.voice_turn_active = False
        self._presence = None


@pytest.fixture(autouse=True)
def _presence_state_path(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "STACKCHAN_PRESENCE_STATE", str(tmp_path / "presence_state.json")
    )


def make_monitor(gateway=None, *, dispatch=None, **kw) -> PresenceMonitor:
    return PresenceMonitor(
        gateway or FakeGateway(),
        dispatch=dispatch or make_dispatch(dict(PRESENT_REG)),
        **kw,
    )


# ---- config helpers --------------------------------------------------


def test_clamp_absent_after_bounds_and_garbage() -> None:
    assert presence._clamp_absent_after(60) == 60
    assert presence._clamp_absent_after(1) == presence.MIN_ABSENT_AFTER_S
    assert presence._clamp_absent_after(10**9) == presence.MAX_ABSENT_AFTER_S
    assert presence._clamp_absent_after("x") == presence.DEFAULT_ABSENT_AFTER_S
    assert presence._clamp_absent_after(None) == presence.DEFAULT_ABSENT_AFTER_S


def test_valid_window() -> None:
    assert presence._valid_window("22:00-06:30") == "22:00-06:30"
    assert presence._valid_window("  off ") == "off"
    assert presence._valid_window("nonsense") is None
    assert presence._valid_window(123) is None


def test_load_config_defaults_when_missing() -> None:
    cfg = presence.load_config()
    assert cfg["absent_after_s"] == presence.DEFAULT_ABSENT_AFTER_S
    assert cfg["sleep_window"] == presence.DEFAULT_SLEEP_WINDOW


def test_save_load_round_trip() -> None:
    presence.save_config({"absent_after_s": 45, "sleep_window": "23:00-07:00"})
    cfg = presence.load_config()
    assert cfg["absent_after_s"] == 45
    assert cfg["sleep_window"] == "23:00-07:00"


def test_save_config_clamps_and_validates() -> None:
    presence.save_config({"absent_after_s": 10**9, "sleep_window": "bad"})
    cfg = presence.load_config()
    assert cfg["absent_after_s"] == presence.MAX_ABSENT_AFTER_S
    assert cfg["sleep_window"] == presence.DEFAULT_SLEEP_WINDOW


# ---- from_env --------------------------------------------------------


def test_from_env_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("STACKCHAN_PRESENCE_POLL_SEC", raising=False)
    assert PresenceMonitor.from_env(FakeGateway()) is None


def test_from_env_invalid_or_nonpositive(monkeypatch) -> None:
    monkeypatch.setenv("STACKCHAN_PRESENCE_POLL_SEC", "abc")
    assert PresenceMonitor.from_env(FakeGateway()) is None
    monkeypatch.setenv("STACKCHAN_PRESENCE_POLL_SEC", "0")
    assert PresenceMonitor.from_env(FakeGateway()) is None


def test_from_env_enabled(monkeypatch) -> None:
    monkeypatch.setenv("STACKCHAN_PRESENCE_POLL_SEC", "10")
    monitor = PresenceMonitor.from_env(FakeGateway())
    assert monitor is not None
    assert monitor._poll_sec == 10.0


# ---- state machine ---------------------------------------------------


@pytest.mark.asyncio
async def test_present_during_waking_hours_is_active() -> None:
    monitor = make_monitor()
    monitor._now = lambda: dt.time(12, 0)
    await monitor._poll_once()
    assert monitor.state is PresenceState.ACTIVE
    assert monitor.allows_heartbeat() is True


@pytest.mark.asyncio
async def test_present_during_sleep_hours_is_quiet_but_allows() -> None:
    monitor = make_monitor(sleep_window="22:00-06:30")
    monitor._now = lambda: dt.time(23, 0)
    await monitor._poll_once()
    assert monitor.state is PresenceState.QUIET
    # QUIET still allows the gate; the heartbeat's own quiet-hours guard
    # is what silences sleeping hours.
    assert monitor.allows_heartbeat() is True


@pytest.mark.asyncio
async def test_unknown_until_first_presence_is_fail_open() -> None:
    monitor = make_monitor(dispatch=make_dispatch(dict(ABSENT_REG)))
    monitor._now = lambda: dt.time(12, 0)
    await monitor._poll_once()
    # Never seen anyone yet -> UNKNOWN (not ABSENT) so the gate stays open.
    assert monitor.state is PresenceState.UNKNOWN
    assert monitor.allows_heartbeat() is True


@pytest.mark.asyncio
async def test_absent_only_after_debounce() -> None:
    clock = [0.0]
    monitor = make_monitor(absent_after_s=120)
    monitor._monotonic = lambda: clock[0]
    monitor._now = lambda: dt.time(12, 0)

    await monitor._poll_once()
    assert monitor.state is PresenceState.ACTIVE

    # Presence lost, but within the debounce window: still ACTIVE.
    monitor._dispatch = make_dispatch(dict(ABSENT_REG))
    clock[0] = 60.0
    await monitor._poll_once()
    assert monitor.state is PresenceState.ACTIVE
    assert monitor.allows_heartbeat() is True

    # Past the debounce window: now ABSENT, gate closes.
    clock[0] = 130.0
    await monitor._poll_once()
    assert monitor.state is PresenceState.ABSENT
    assert monitor.allows_heartbeat() is False


@pytest.mark.asyncio
async def test_disconnected_device_stays_unknown() -> None:
    monitor = make_monitor(FakeGateway(connected=False))
    await monitor._poll_once()
    assert monitor.state is PresenceState.UNKNOWN
    assert monitor.allows_heartbeat() is True


@pytest.mark.asyncio
async def test_sensor_errors_fall_back_to_unknown_after_threshold() -> None:
    monitor = make_monitor()
    monitor._now = lambda: dt.time(12, 0)
    await monitor._poll_once()
    assert monitor.state is PresenceState.ACTIVE

    # Reads start failing: hold the last state for a couple of ticks, then
    # fall open to UNKNOWN once the failures pile up.
    monitor._dispatch = make_dispatch({}, fail_addr=0x5A)
    await monitor._poll_once()
    assert monitor.state is PresenceState.ACTIVE
    await monitor._poll_once()
    assert monitor.state is PresenceState.ACTIVE
    await monitor._poll_once()
    assert monitor.state is PresenceState.UNKNOWN
    assert monitor.allows_heartbeat() is True


@pytest.mark.asyncio
async def test_recovers_after_errors() -> None:
    monitor = make_monitor(dispatch=make_dispatch({}, fail_addr=0x5A))
    monitor._now = lambda: dt.time(12, 0)
    for _ in range(3):
        await monitor._poll_once()
    assert monitor.state is PresenceState.UNKNOWN
    # Sensor comes back: a clean read re-occupies the room.
    monitor._dispatch = make_dispatch(dict(PRESENT_REG))
    await monitor._poll_once()
    assert monitor.state is PresenceState.ACTIVE


# ---- update_config ---------------------------------------------------


@pytest.mark.asyncio
async def test_update_config_persists_and_reapplies() -> None:
    clock = [0.0]
    monitor = make_monitor(absent_after_s=120)
    monitor._monotonic = lambda: clock[0]
    monitor._now = lambda: dt.time(12, 0)

    await monitor._poll_once()  # ACTIVE, last seen at t=0
    monitor._dispatch = make_dispatch(dict(ABSENT_REG))
    clock[0] = 60.0
    await monitor._poll_once()
    assert monitor.state is PresenceState.ACTIVE  # 60 < 120

    # Tightening the debounce to 30 s flips the room ABSENT right now.
    result = monitor.update_config(absent_after_s=30)
    assert result["ok"] is True
    assert monitor.state is PresenceState.ABSENT
    assert presence.load_config()["absent_after_s"] == 30


@pytest.mark.asyncio
async def test_update_config_window_switches_mode() -> None:
    monitor = make_monitor(sleep_window="22:00-06:30")
    monitor._now = lambda: dt.time(12, 0)
    await monitor._poll_once()
    assert monitor.state is PresenceState.ACTIVE
    # Make noon a sleeping hour -> the occupied room becomes QUIET at once.
    monitor.update_config(sleep_window="11:00-13:00")
    assert monitor.state is PresenceState.QUIET


def test_update_config_rejects_bad_window() -> None:
    monitor = make_monitor()
    result = monitor.update_config(sleep_window="nonsense")
    assert result["ok"] is False
    assert monitor._sleep_window == presence.DEFAULT_SLEEP_WINDOW


# ---- snapshot --------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_shape() -> None:
    clock = [100.0]
    monitor = make_monitor()
    monitor._monotonic = lambda: clock[0]
    monitor._now = lambda: dt.time(12, 0)
    await monitor._poll_once()
    clock[0] = 105.0
    snap = monitor.snapshot()
    assert snap["enabled"] is True
    assert snap["state"] == "active"
    assert snap["allows_heartbeat"] is True
    assert snap["last_seen_s_ago"] == 5.0
    assert snap["config"]["absent_after_s"] == 120
    assert snap["tmos"]["present"] is True


# ---- sleep latch (static sleeper bridged through detection gaps) ------


@pytest.mark.asyncio
async def test_sleep_latch_holds_quiet_across_gap() -> None:
    # Confirm presence in the sleeping window, then lose it for far longer
    # than the debounce: the latch must hold QUIET, not flip to ABSENT.
    clock = [0.0]
    monitor = make_monitor(absent_after_s=120, sleep_window="22:00-06:30")
    monitor._monotonic = lambda: clock[0]
    monitor._now = lambda: dt.time(23, 0)

    await monitor._poll_once()
    assert monitor.state is PresenceState.QUIET
    assert monitor._asleep is True

    monitor._dispatch = make_dispatch(dict(ABSENT_REG))
    clock[0] = 1000.0  # well past the 120 s debounce
    await monitor._poll_once()
    assert monitor.state is PresenceState.QUIET
    assert monitor.allows_heartbeat() is True


@pytest.mark.asyncio
async def test_sleep_latch_clears_on_waking() -> None:
    # Latched asleep, then morning arrives with an empty room -> ABSENT.
    clock = [0.0]
    monitor = make_monitor(absent_after_s=120, sleep_window="22:00-06:30")
    monitor._monotonic = lambda: clock[0]
    monitor._now = lambda: dt.time(23, 0)
    await monitor._poll_once()
    assert monitor._asleep is True

    monitor._dispatch = make_dispatch(dict(ABSENT_REG))
    monitor._now = lambda: dt.time(12, 0)  # waking hours
    clock[0] = 1000.0
    await monitor._poll_once()
    assert monitor._asleep is False
    assert monitor.state is PresenceState.ABSENT
    assert monitor.allows_heartbeat() is False


@pytest.mark.asyncio
async def test_no_latch_when_away_into_night() -> None:
    # Present in the evening, then gone before the sleeping window and all
    # night: presence is never confirmed in the window, so the latch never
    # sets -> ABSENT. This is how sleep and away-overnight stay distinct.
    clock = [0.0]
    monitor = make_monitor(absent_after_s=120, sleep_window="22:00-06:30")
    monitor._monotonic = lambda: clock[0]
    monitor._now = lambda: dt.time(21, 0)  # waking, present
    await monitor._poll_once()
    assert monitor.state is PresenceState.ACTIVE
    assert monitor._asleep is False

    monitor._dispatch = make_dispatch(dict(ABSENT_REG))
    monitor._now = lambda: dt.time(23, 0)  # into the sleeping window, empty
    clock[0] = 1000.0
    await monitor._poll_once()
    assert monitor._asleep is False
    assert monitor.state is PresenceState.ABSENT


@pytest.mark.asyncio
async def test_snapshot_exposes_asleep() -> None:
    monitor = make_monitor(sleep_window="22:00-06:30")
    monitor._now = lambda: dt.time(23, 0)
    await monitor._poll_once()
    snap = monitor.snapshot()
    assert snap["asleep"] is True
    assert snap["state"] == "quiet"


# ---- start / stop ----------------------------------------------------


@pytest.mark.asyncio
async def test_start_stop_idempotent() -> None:
    monitor = make_monitor(poll_sec=1.0)
    monitor.start()
    monitor.start()  # second start is a no-op
    await monitor.stop()
    await monitor.stop()  # second stop is a no-op


# ---- heartbeat occupancy gate ----------------------------------------


def test_heartbeat_skipped_when_room_empty() -> None:
    gw = FakeGateway()
    monitor = make_monitor(gw)
    monitor._state = PresenceState.ABSENT
    gw._presence = monitor
    runner = HeartbeatRunner(gw, interval_min=30.0)
    assert runner._skip_reason() == "room empty"


def test_heartbeat_allowed_when_present() -> None:
    gw = FakeGateway()
    monitor = make_monitor(gw)
    monitor._state = PresenceState.ACTIVE
    gw._presence = monitor
    runner = HeartbeatRunner(gw, interval_min=30.0)
    assert runner._skip_reason() is None


def test_heartbeat_allowed_when_unknown_fail_open() -> None:
    gw = FakeGateway()
    monitor = make_monitor(gw)
    monitor._state = PresenceState.UNKNOWN
    gw._presence = monitor
    runner = HeartbeatRunner(gw, interval_min=30.0)
    assert runner._skip_reason() is None


def test_heartbeat_no_gate_without_monitor() -> None:
    gw = FakeGateway()  # _presence is None
    runner = HeartbeatRunner(gw, interval_min=30.0)
    assert runner._skip_reason() is None


# ---- get_presence MCP tool (Hermes reads room state) -----------------


@pytest.mark.asyncio
async def test_get_presence_tool_returns_snapshot() -> None:
    from stackchan_mcp.stdio_server import _dispatch_mcp_tool
    gw = FakeGateway()
    monitor = make_monitor(gw)
    monitor._state = PresenceState.ACTIVE
    gw._presence = monitor
    content = await _dispatch_mcp_tool("get_presence", {}, gw)
    payload = json.loads(content[0].text)
    assert payload["enabled"] is True
    assert payload["state"] == "active"


@pytest.mark.asyncio
async def test_get_presence_tool_disabled_without_monitor() -> None:
    from stackchan_mcp.stdio_server import _dispatch_mcp_tool
    gw = FakeGateway()  # _presence is None
    content = await _dispatch_mcp_tool("get_presence", {}, gw)
    assert json.loads(content[0].text) == {"enabled": False}


# ---- presence log (raw TMOS time series for offline analysis) ---------


def test_resolve_log_path(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("STACKCHAN_PRESENCE_LOG", raising=False)
    assert presence._resolve_log_path() == Path(presence.DEFAULT_LOG_PATH).expanduser()
    monkeypatch.setenv("STACKCHAN_PRESENCE_LOG", "off")
    assert presence._resolve_log_path() is None
    monkeypatch.setenv("STACKCHAN_PRESENCE_LOG", "  ")
    assert presence._resolve_log_path() is None
    target = tmp_path / "p.jsonl"
    monkeypatch.setenv("STACKCHAN_PRESENCE_LOG", str(target))
    assert presence._resolve_log_path() == target


def test_from_env_sets_log_path(monkeypatch, tmp_path) -> None:
    target = tmp_path / "p.jsonl"
    monkeypatch.setenv("STACKCHAN_PRESENCE_POLL_SEC", "10")
    monkeypatch.setenv("STACKCHAN_PRESENCE_LOG", str(target))
    monitor = PresenceMonitor.from_env(FakeGateway())
    assert monitor is not None
    assert monitor._log_path == target


@pytest.mark.asyncio
async def test_poll_appends_full_tmos_line(tmp_path) -> None:
    log = tmp_path / "presence_log.jsonl"
    monitor = make_monitor(log_path=log)
    monitor._now = lambda: dt.time(12, 0)
    await monitor._poll_once()
    lines = log.read_text("utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    # Every TMOS field is recorded, plus the derived fields.
    for key in (
        "present", "presence", "motion", "pres_flag", "mot_flag",
        "shk_flag", "object_raw", "ambient_c",
    ):
        assert key in rec
    assert rec["state"] == "active"
    assert rec["asleep"] is False  # derived latch field is logged too
    assert rec["present"] is True
    assert isinstance(rec["ts_unix"], float)
    # A second poll appends, not overwrites.
    await monitor._poll_once()
    assert len(log.read_text("utf-8").splitlines()) == 2


@pytest.mark.asyncio
async def test_poll_logs_absent_rows(tmp_path) -> None:
    # An empty room must still be logged (the gaps are the whole point).
    log = tmp_path / "presence_log.jsonl"
    monitor = make_monitor(dispatch=make_dispatch(dict(ABSENT_REG)), log_path=log)
    monitor._now = lambda: dt.time(12, 0)
    await monitor._poll_once()
    rec = json.loads(log.read_text("utf-8").splitlines()[0])
    assert rec["present"] is False
    assert rec["state"] == "unknown"  # never seen anyone yet (fail-open)


@pytest.mark.asyncio
async def test_poll_skips_log_on_sensor_error(tmp_path) -> None:
    log = tmp_path / "presence_log.jsonl"
    monitor = make_monitor(dispatch=make_dispatch({}, fail_addr=0x5A), log_path=log)
    await monitor._poll_once()
    assert not log.exists()


@pytest.mark.asyncio
async def test_poll_no_log_when_disabled() -> None:
    # log_path defaults to None -> _poll_once writes nothing and never raises.
    monitor = make_monitor()
    monitor._now = lambda: dt.time(12, 0)
    assert monitor._log_path is None
    await monitor._poll_once()  # must not raise


@pytest.mark.asyncio
async def test_start_prunes_old_log_entries(tmp_path) -> None:
    log = tmp_path / "presence_log.jsonl"
    # An entry from epoch 1.0 is far older than the 7-day retention window.
    log.write_text(json.dumps({"ts_unix": 1.0, "state": "active"}) + "\n", "utf-8")
    monitor = make_monitor(poll_sec=1.0, log_path=log)
    monitor.start()  # rotation runs synchronously before the poll loop
    await monitor.stop()
    assert log.read_text("utf-8") == ""


@pytest.mark.asyncio
async def test_start_uses_longer_presence_retention(tmp_path) -> None:
    # Presence keeps PRESENCE_RETENTION_DAYS (28), longer than the event-log
    # default of 7, so a ~20-day-old entry survives startup rotation.
    assert presence.PRESENCE_RETENTION_DAYS == 28
    log = tmp_path / "presence_log.jsonl"
    twenty_days_ago = time.time() - 20 * 24 * 60 * 60
    log.write_text(
        json.dumps({"ts_unix": twenty_days_ago, "state": "active"}) + "\n", "utf-8"
    )
    monitor = make_monitor(poll_sec=1.0, log_path=log)
    monitor.start()  # rotation runs synchronously before the poll loop
    await monitor.stop()
    assert len(log.read_text("utf-8").splitlines()) == 1


# ---- self-diagnostic report ------------------------------------------


def test_build_report_reads_recent_days(tmp_path) -> None:
    log = tmp_path / "presence_log.jsonl"
    now = 1_000_000.0
    # One record per day going back 0..9 days; days=7 keeps i in 0..7 (ts at
    # the cutoff boundary is inclusive), so 8 records survive.
    lines = [
        json.dumps(
            {
                "ts_unix": now - i * 86400,
                "state": "active",
                "present": True,
                "pres_flag": True,
                "presence": 800,
            }
        )
        for i in range(10)
    ]
    log.write_text("\n".join(lines) + "\n", "utf-8")
    monitor = make_monitor(log_path=log)
    monitor._wall_clock = lambda: now
    report = monitor.build_report(days=7)
    assert report["empty"] is False
    assert report["basic"]["samples"] == 8
    report3 = monitor.build_report(days=3)
    assert report3["basic"]["samples"] == 4  # i in 0..3


def test_build_report_empty_when_log_disabled() -> None:
    monitor = make_monitor()  # log_path defaults to None
    assert monitor._log_path is None
    report = monitor.build_report(days=7)
    assert report["empty"] is True


def test_build_report_passes_current_absent_after_s(tmp_path) -> None:
    log = tmp_path / "presence_log.jsonl"
    now = 1_000_000.0
    # An ACTIVE valley so a recommendation is produced and can be compared.
    rows = [
        {"ts_unix": now - 50, "state": "active", "present": True, "pres_flag": True},
        {"ts_unix": now - 40, "state": "active", "present": False, "pres_flag": False},
        {"ts_unix": now - 10, "state": "active", "present": True, "pres_flag": True},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", "utf-8")
    monitor = make_monitor(log_path=log, absent_after_s=1080)
    monitor._wall_clock = lambda: now
    report = monitor.build_report(days=7)
    assert report["recommendation"]["current_absent_after_s"] == 1080


# ---- daily report writer ---------------------------------------------


def test_resolve_report_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("STACKCHAN_PRESENCE_REPORT", raising=False)
    assert (
        presence._resolve_report_dir()
        == Path(presence.DEFAULT_REPORT_DIR).expanduser()
    )
    monkeypatch.setenv("STACKCHAN_PRESENCE_REPORT", "off")
    assert presence._resolve_report_dir() is None
    monkeypatch.setenv("STACKCHAN_PRESENCE_REPORT", "  ")
    assert presence._resolve_report_dir() is None
    target = tmp_path / "rep"
    monkeypatch.setenv("STACKCHAN_PRESENCE_REPORT", str(target))
    assert presence._resolve_report_dir() == target


def test_from_env_report_gated_on_log(monkeypatch, tmp_path) -> None:
    # With the log disabled there is nothing to aggregate, so the report
    # dir is None even when STACKCHAN_PRESENCE_REPORT points somewhere.
    monkeypatch.setenv("STACKCHAN_PRESENCE_POLL_SEC", "10")
    monkeypatch.setenv("STACKCHAN_PRESENCE_LOG", "off")
    monkeypatch.setenv("STACKCHAN_PRESENCE_REPORT", str(tmp_path / "rep"))
    monitor = PresenceMonitor.from_env(FakeGateway())
    assert monitor is not None
    assert monitor._report_dir is None


@pytest.mark.asyncio
async def test_daily_report_written_once_per_day(tmp_path) -> None:
    log = tmp_path / "presence_log.jsonl"
    reports = tmp_path / "reports"
    monitor = make_monitor(log_path=log, report_dir=reports)
    monitor._now = lambda: dt.time(12, 0)
    monitor._today = lambda: dt.date(2026, 6, 24)
    await monitor._poll_once()
    md = reports / "2026-06-24.md"
    assert md.exists()
    assert (reports / "2026-06-24.json").exists()
    # Same day again: even after deleting the file, the in-memory guard
    # prevents a rebuild (the once-a-day write does not re-fire).
    md.unlink()
    await monitor._poll_once()
    assert not md.exists()
    # A new day writes a fresh report.
    monitor._today = lambda: dt.date(2026, 6, 25)
    await monitor._poll_once()
    assert (reports / "2026-06-25.md").exists()


@pytest.mark.asyncio
async def test_daily_report_skips_when_file_exists(tmp_path) -> None:
    # Restart-safe: an existing file for today is not overwritten.
    log = tmp_path / "presence_log.jsonl"
    reports = tmp_path / "reports"
    reports.mkdir()
    md = reports / "2026-06-24.md"
    md.write_text("SENTINEL", "utf-8")
    monitor = make_monitor(log_path=log, report_dir=reports)
    monitor._now = lambda: dt.time(12, 0)
    monitor._today = lambda: dt.date(2026, 6, 24)
    await monitor._poll_once()
    assert md.read_text("utf-8") == "SENTINEL"


@pytest.mark.asyncio
async def test_daily_report_noop_when_disabled(tmp_path) -> None:
    log = tmp_path / "presence_log.jsonl"
    monitor = make_monitor(log_path=log)  # report_dir defaults to None
    monitor._now = lambda: dt.time(12, 0)
    monitor._today = lambda: dt.date(2026, 6, 24)
    assert monitor._report_dir is None
    await monitor._poll_once()  # must not raise


@pytest.mark.asyncio
async def test_daily_report_write_error_does_not_kill_poll(
    tmp_path, monkeypatch
) -> None:
    log = tmp_path / "presence_log.jsonl"
    reports = tmp_path / "reports"
    monitor = make_monitor(log_path=log, report_dir=reports)
    monitor._now = lambda: dt.time(12, 0)
    monitor._today = lambda: dt.date(2026, 6, 24)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(presence, "_atomic_write_text", boom)
    await monitor._poll_once()  # swallowed; no raise


# ---- state-change observers (proactive hook) -------------------------


async def _drain() -> None:
    """Let the detached _fire_change task(s) run to completion."""
    for _ in range(3):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_register_on_state_change_fires_on_flip() -> None:
    seen: list[tuple[PresenceState, PresenceState]] = []
    clock = [0.0]
    monitor = make_monitor(absent_after_s=120)
    monitor._monotonic = lambda: clock[0]
    monitor._now = lambda: dt.time(12, 0)
    monitor.register_on_state_change(lambda old, new: seen.append((old, new)))

    await monitor._poll_once()  # UNKNOWN -> ACTIVE
    await _drain()
    monitor._dispatch = make_dispatch(dict(ABSENT_REG))
    clock[0] = 130.0
    await monitor._poll_once()  # ACTIVE -> ABSENT (debounce elapsed)
    await _drain()

    assert seen == [
        (PresenceState.UNKNOWN, PresenceState.ACTIVE),
        (PresenceState.ACTIVE, PresenceState.ABSENT),
    ]


@pytest.mark.asyncio
async def test_no_callback_when_state_unchanged() -> None:
    seen: list[tuple] = []
    monitor = make_monitor()
    monitor._now = lambda: dt.time(12, 0)
    monitor.register_on_state_change(lambda old, new: seen.append((old, new)))

    await monitor._poll_once()  # UNKNOWN -> ACTIVE (one flip)
    await _drain()
    await monitor._poll_once()  # ACTIVE -> ACTIVE (no flip)
    await _drain()

    assert seen == [(PresenceState.UNKNOWN, PresenceState.ACTIVE)]


@pytest.mark.asyncio
async def test_update_config_does_not_notify() -> None:
    # A dashboard threshold re-tune flips the state but must stay silent —
    # it is not a real occupancy event (would mis-fire おかえり/おはよう).
    seen: list[tuple] = []
    clock = [0.0]
    monitor = make_monitor(absent_after_s=120)
    monitor._monotonic = lambda: clock[0]
    monitor._now = lambda: dt.time(12, 0)
    await monitor._poll_once()  # ACTIVE, last seen at t=0
    await _drain()
    monitor.register_on_state_change(lambda old, new: seen.append((old, new)))

    clock[0] = 200.0  # now well past a shrunken debounce
    monitor.update_config(absent_after_s=5)  # recomputes -> ABSENT
    await _drain()

    assert monitor.state is PresenceState.ABSENT
    assert seen == []  # notify=False: no observer fired


@pytest.mark.asyncio
async def test_callback_exception_does_not_break_others() -> None:
    seen: list[tuple] = []

    def boom(old, new):
        raise RuntimeError("observer blew up")

    monitor = make_monitor()
    monitor._now = lambda: dt.time(12, 0)
    monitor.register_on_state_change(boom)
    monitor.register_on_state_change(lambda old, new: seen.append((old, new)))

    await monitor._poll_once()  # UNKNOWN -> ACTIVE
    await _drain()

    # The raising observer is swallowed; the second one still runs.
    assert seen == [(PresenceState.UNKNOWN, PresenceState.ACTIVE)]


@pytest.mark.asyncio
async def test_async_callback_is_awaited() -> None:
    seen: list[tuple] = []

    async def record(old, new):
        seen.append((old, new))

    monitor = make_monitor()
    monitor._now = lambda: dt.time(12, 0)
    monitor.register_on_state_change(record)

    await monitor._poll_once()
    await _drain()

    assert seen == [(PresenceState.UNKNOWN, PresenceState.ACTIVE)]
