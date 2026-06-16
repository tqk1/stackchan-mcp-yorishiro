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
