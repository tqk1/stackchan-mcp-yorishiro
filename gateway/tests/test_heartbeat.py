"""Tests for the Phase D heartbeat (silent idle gestures)."""

import asyncio
import datetime as dt
import json
import random

import pytest

from stackchan_mcp import heartbeat as hb
from stackchan_mcp.heartbeat import (
    HeartbeatRunner,
    compute_delay_s,
    is_quiet,
    parse_quiet_hours,
)


class FakeESP32:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.device_connected = True
        self.tts_lock = asyncio.Lock()
        self.angles = {"yaw": 10, "pitch": 40}
        self.fail_angles = False

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "self.robot.get_head_angles":
            if self.fail_angles:
                return None, {"code": -32000, "message": "boom"}
            return (
                {"content": [{"type": "text", "text": json.dumps(self.angles)}]},
                None,
            )
        return {"ok": True}, None


class FakeGateway:
    def __init__(self):
        self.esp32 = FakeESP32()


def make_runner(gateway=None, **kw) -> HeartbeatRunner:
    kw.setdefault("interval_min", 30.0)
    kw.setdefault("rng", random.Random(42))
    return HeartbeatRunner(gateway or FakeGateway(), **kw)


# ---- parse_quiet_hours / is_quiet ----------------------------------


def test_parse_quiet_hours_normal():
    assert parse_quiet_hours("22:00-08:00") == (dt.time(22, 0), dt.time(8, 0))


def test_parse_quiet_hours_off_and_empty():
    assert parse_quiet_hours("off") is None
    assert parse_quiet_hours("") is None
    assert parse_quiet_hours("  OFF ") is None


def test_parse_quiet_hours_malformed_raises():
    # "22-08" stays valid: time.fromisoformat("22") == 22:00 on 3.11+.
    with pytest.raises(ValueError):
        parse_quiet_hours("nonsense")
    with pytest.raises(ValueError):
        parse_quiet_hours("25:00-08:00")


def test_is_quiet_midnight_crossing():
    quiet = (dt.time(22, 0), dt.time(8, 0))
    assert is_quiet(dt.time(23, 0), quiet)
    assert is_quiet(dt.time(3, 0), quiet)
    assert is_quiet(dt.time(22, 0), quiet)
    assert not is_quiet(dt.time(12, 0), quiet)
    assert not is_quiet(dt.time(8, 0), quiet)


def test_is_quiet_same_day_range_and_disabled():
    quiet = (dt.time(13, 0), dt.time(15, 0))
    assert is_quiet(dt.time(14, 0), quiet)
    assert not is_quiet(dt.time(16, 0), quiet)
    assert not is_quiet(dt.time(0, 0), None)


# ---- compute_delay_s ------------------------------------------------


def test_compute_delay_within_jitter_band():
    rng = random.Random(1)
    for _ in range(50):
        d = compute_delay_s(30.0, 0.25, rng)
        assert 30 * 60 * 0.75 <= d <= 30 * 60 * 1.25


def test_compute_delay_floor():
    rng = random.Random(1)
    assert compute_delay_s(0.01, 0.0, rng) == 10.0


# ---- from_env --------------------------------------------------------


def test_from_env_disabled_by_default(monkeypatch):
    monkeypatch.delenv("STACKCHAN_HEARTBEAT_INTERVAL_MIN", raising=False)
    assert HeartbeatRunner.from_env(FakeGateway()) is None


def test_from_env_invalid_or_nonpositive(monkeypatch):
    monkeypatch.setenv("STACKCHAN_HEARTBEAT_INTERVAL_MIN", "abc")
    assert HeartbeatRunner.from_env(FakeGateway()) is None
    monkeypatch.setenv("STACKCHAN_HEARTBEAT_INTERVAL_MIN", "0")
    assert HeartbeatRunner.from_env(FakeGateway()) is None


def test_from_env_enabled(monkeypatch):
    monkeypatch.setenv("STACKCHAN_HEARTBEAT_INTERVAL_MIN", "45")
    monkeypatch.setenv("STACKCHAN_HEARTBEAT_JITTER", "5")  # clamped to 0.9
    monkeypatch.setenv("STACKCHAN_HEARTBEAT_QUIET", "off")
    runner = HeartbeatRunner.from_env(FakeGateway())
    assert runner is not None
    assert runner._interval_min == 45
    assert runner._jitter == 0.9
    assert runner._quiet is None


# ---- guards ----------------------------------------------------------


def test_skip_when_disconnected():
    runner = make_runner()
    runner._gateway.esp32.device_connected = False
    assert runner._skip_reason() == "no device connected"


@pytest.mark.asyncio
async def test_skip_when_audio_busy():
    runner = make_runner()
    async with runner._gateway.esp32.tts_lock:
        assert runner._skip_reason() == "audio pipeline busy"
    assert runner._skip_reason() is None


def test_skip_in_quiet_hours(monkeypatch):
    runner = make_runner(quiet=(dt.time(22, 0), dt.time(8, 0)))
    monkeypatch.setattr(runner, "_now", lambda: dt.time(23, 30))
    assert runner._skip_reason() == "quiet hours"
    monkeypatch.setattr(runner, "_now", lambda: dt.time(12, 0))
    assert runner._skip_reason() is None


# ---- gestures --------------------------------------------------------


@pytest.mark.asyncio
async def test_gesture_expression_returns_to_idle():
    gw = FakeGateway()
    runner = make_runner(gw)
    await runner._gesture_expression()
    faces = [a["face"] for n, a in gw.esp32.calls if n == "self.display.set_avatar"]
    assert len(faces) == 2
    assert faces[-1] == "idle"


@pytest.mark.asyncio
async def test_gesture_glance_restores_home_angles():
    gw = FakeGateway()
    runner = make_runner(gw)
    await runner._gesture_glance()
    moves = [a for n, a in gw.esp32.calls if n == "self.robot.set_head_angles"]
    assert moves, "glance should move the head when angles are readable"
    assert moves[-1] == {"yaw": 10, "pitch": 40}
    faces = [a["face"] for n, a in gw.esp32.calls if n == "self.display.set_avatar"]
    assert faces[-1] == "idle"


@pytest.mark.asyncio
async def test_gesture_glance_without_angles_skips_head():
    gw = FakeGateway()
    gw.esp32.fail_angles = True
    runner = make_runner(gw)
    await runner._gesture_glance()
    moves = [a for n, a in gw.esp32.calls if n == "self.robot.set_head_angles"]
    assert moves == []
    faces = [a["face"] for n, a in gw.esp32.calls if n == "self.display.set_avatar"]
    assert faces[-1] == "idle"


@pytest.mark.asyncio
async def test_gesture_nod_keeps_pitch_in_range():
    gw = FakeGateway()
    gw.esp32.angles = {"yaw": 0, "pitch": 6}  # near lower bound
    runner = make_runner(gw)
    await runner._gesture_nod()
    moves = [a for n, a in gw.esp32.calls if n == "self.robot.set_head_angles"]
    assert moves
    assert all(5 <= a["pitch"] <= 85 for a in moves)
    assert moves[-1] == {"yaw": 0, "pitch": 6}


@pytest.mark.asyncio
async def test_read_head_angles_bad_payload():
    gw = FakeGateway()
    runner = make_runner(gw)

    async def weird(name, arguments):
        return {"content": [{"type": "text", "text": "not json"}]}, None

    gw.esp32.call_tool = weird
    assert await runner._read_head_angles() is None


# ---- loop lifecycle --------------------------------------------------


@pytest.mark.asyncio
async def test_loop_ticks_and_stops(monkeypatch):
    gw = FakeGateway()
    runner = make_runner(gw, interval_min=1.0, jitter=0.0)

    ticked = asyncio.Event()
    real_sleep = asyncio.sleep

    async def fast_sleep(delay):
        # The scheduling sleep is >= 10 s; gesture-internal sleeps are
        # short. Collapse both to keep the test instant.
        await real_sleep(0)

    monkeypatch.setattr(hb.asyncio, "sleep", fast_sleep)

    async def one_gesture():
        ticked.set()

    monkeypatch.setattr(runner, "_perform_gesture", one_gesture)

    runner.start()
    await asyncio.wait_for(ticked.wait(), timeout=2.0)
    await runner.stop()
    assert runner._task is None


@pytest.mark.asyncio
async def test_loop_survives_gesture_failure(monkeypatch):
    gw = FakeGateway()
    runner = make_runner(gw, interval_min=1.0, jitter=0.0)

    real_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await real_sleep(0)

    monkeypatch.setattr(hb.asyncio, "sleep", fast_sleep)

    count = 0
    second_tick = asyncio.Event()

    async def flaky():
        nonlocal count
        count += 1
        if count == 1:
            raise RuntimeError("boom")
        second_tick.set()

    monkeypatch.setattr(runner, "_perform_gesture", flaky)

    runner.start()
    await asyncio.wait_for(second_tick.wait(), timeout=2.0)
    await runner.stop()
    assert count >= 2
