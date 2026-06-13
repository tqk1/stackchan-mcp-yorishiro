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
        self.last_human_interaction_monotonic = None


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


# ---- Phase E: speak config -------------------------------------------


def _clear_speak_env(monkeypatch):
    for name in (
        "STACKCHAN_HEARTBEAT_SPEAK",
        "STACKCHAN_HEARTBEAT_SPEAK_COOLDOWN_MIN",
        "STACKCHAN_HEARTBEAT_SPEAK_MAX_PER_DAY",
        "STACKCHAN_WEATHER_AREA",
        "STACKCHAN_WEATHER_CITY",
        "STACKCHAN_WEATHER_POP_THRESHOLD",
        "STACKCHAN_WEATHER_WINDOW",
        "STACKCHAN_MEMO_WINDOW",
        "STACKCHAN_HEARTBEAT_STATE",
        "STACKCHAN_HEARTBEAT_GESTURES",
    ):
        monkeypatch.delenv(name, raising=False)


def test_speak_config_off_by_default(monkeypatch):
    _clear_speak_env(monkeypatch)
    assert hb.speak_config_from_env() is None
    monkeypatch.setenv("STACKCHAN_HEARTBEAT_SPEAK", "0")
    assert hb.speak_config_from_env() is None
    monkeypatch.setenv("STACKCHAN_HEARTBEAT_SPEAK", "nonsense")
    assert hb.speak_config_from_env() is None


def test_speak_config_enabled_defaults(monkeypatch, tmp_path):
    _clear_speak_env(monkeypatch)
    monkeypatch.setenv("STACKCHAN_HEARTBEAT_SPEAK", "1")
    monkeypatch.setenv("STACKCHAN_HEARTBEAT_STATE", str(tmp_path / "s.json"))
    cfg = hb.speak_config_from_env()
    assert cfg is not None
    assert cfg.cooldown_min == hb.DEFAULT_SPEAK_COOLDOWN_MIN
    assert cfg.max_per_day == hb.DEFAULT_SPEAK_MAX_PER_DAY
    assert cfg.pop_threshold == hb.DEFAULT_POP_THRESHOLD
    assert cfg.weather_window == (dt.time(6, 30), dt.time(9, 30))
    assert cfg.memo_window == (dt.time(18, 0), dt.time(21, 0))
    assert cfg.weather_area == ""  # weather source off until codes are set


def test_speak_config_invalid_numbers_fall_back(monkeypatch, tmp_path):
    _clear_speak_env(monkeypatch)
    monkeypatch.setenv("STACKCHAN_HEARTBEAT_SPEAK", "1")
    monkeypatch.setenv("STACKCHAN_HEARTBEAT_STATE", str(tmp_path / "s.json"))
    monkeypatch.setenv("STACKCHAN_HEARTBEAT_SPEAK_COOLDOWN_MIN", "abc")
    monkeypatch.setenv("STACKCHAN_HEARTBEAT_SPEAK_MAX_PER_DAY", "-1")
    cfg = hb.speak_config_from_env()
    assert cfg.cooldown_min == hb.DEFAULT_SPEAK_COOLDOWN_MIN
    assert cfg.max_per_day == 0  # clamped, never negative


def test_from_env_speak_off_keeps_stage1(monkeypatch):
    """Regression: SPEAK unset must reproduce Phase D behaviour exactly."""
    _clear_speak_env(monkeypatch)
    monkeypatch.setenv("STACKCHAN_HEARTBEAT_INTERVAL_MIN", "30")
    runner = HeartbeatRunner.from_env(FakeGateway())
    assert runner is not None
    assert runner._speak is None
    assert runner._gestures is True


def test_from_env_gestures_off(monkeypatch):
    _clear_speak_env(monkeypatch)
    monkeypatch.setenv("STACKCHAN_HEARTBEAT_INTERVAL_MIN", "30")
    monkeypatch.setenv("STACKCHAN_HEARTBEAT_GESTURES", "0")
    runner = HeartbeatRunner.from_env(FakeGateway())
    assert runner._gestures is False


# ---- Phase F: runtime gesture toggle ---------------------------------


def test_set_gestures_toggles_runtime_flag():
    runner = make_runner(gestures=True)
    assert runner.gestures_enabled is True
    runner.set_gestures(False)
    assert runner.gestures_enabled is False
    assert runner._gestures is False
    runner.set_gestures(True)
    assert runner.gestures_enabled is True


# ---- Phase E: suppression --------------------------------------------


def make_speak(tmp_path, **kw) -> hb.SpeakConfig:
    kw.setdefault("state_path", tmp_path / "state.json")
    kw.setdefault("weather_window", (dt.time(6, 30), dt.time(9, 30)))
    kw.setdefault("memo_window", (dt.time(18, 0), dt.time(21, 0)))
    return hb.SpeakConfig(**kw)


def test_speak_skip_while_recording(monkeypatch, tmp_path):
    runner = make_runner(speak=make_speak(tmp_path))
    monkeypatch.setattr(hb, "is_recording", lambda: True)
    assert runner._speak_skip_reason() == "recording active"
    monkeypatch.setattr(hb, "is_recording", lambda: False)
    assert runner._speak_skip_reason() is None


def test_speak_skip_recent_interaction(monkeypatch, tmp_path):
    gw = FakeGateway()
    runner = make_runner(gw, speak=make_speak(tmp_path, cooldown_min=20))
    monkeypatch.setattr(hb, "is_recording", lambda: False)
    monkeypatch.setattr(runner, "_monotonic", lambda: 10_000.0)

    gw.last_human_interaction_monotonic = 10_000.0 - 5 * 60  # 5 min ago
    assert runner._speak_skip_reason() == "recent interaction"
    gw.last_human_interaction_monotonic = 10_000.0 - 25 * 60  # 25 min ago
    assert runner._speak_skip_reason() is None
    gw.last_human_interaction_monotonic = None  # never interacted
    assert runner._speak_skip_reason() is None


def test_speak_skip_daily_cap_and_rollover(monkeypatch, tmp_path):
    runner = make_runner(speak=make_speak(tmp_path, max_per_day=2))
    monkeypatch.setattr(hb, "is_recording", lambda: False)
    today = dt.date(2026, 6, 12)
    monkeypatch.setattr(runner, "_today", lambda: today)
    runner._state = {"speak_count_date": "2026-06-12", "speak_count": 2}
    assert runner._speak_skip_reason() == "daily cap"
    monkeypatch.setattr(runner, "_today", lambda: dt.date(2026, 6, 13))
    assert runner._speak_skip_reason() is None  # new day resets the count


# ---- Phase E: memo checker -------------------------------------------


@pytest.fixture
def notes_dir(monkeypatch, tmp_path):
    d = tmp_path / "notes"
    d.mkdir()
    monkeypatch.setenv("STACKCHAN_NOTES_DIR", str(d))
    return d


def make_memo_runner(tmp_path, monkeypatch, now=dt.time(19, 0), **speak_kw):
    runner = make_runner(speak=make_speak(tmp_path, **speak_kw))
    monkeypatch.setattr(runner, "_now", lambda: now)
    return runner


@pytest.mark.asyncio
async def test_memo_reminds_today_note(notes_dir, tmp_path, monkeypatch):
    (notes_dir / "メモ.md").write_text("牛乳を買う\n詳細...", "utf-8")
    runner = make_memo_runner(tmp_path, monkeypatch)
    line = await runner._check_memo()
    assert line is not None
    assert "牛乳を買う" in line
    # Same day: already reminded → silence, also across a restart.
    assert await runner._check_memo() is None
    fresh = make_memo_runner(tmp_path, monkeypatch)
    assert fresh._state.get("memo_done") == dt.date.today().isoformat()
    assert await fresh._check_memo() is None


@pytest.mark.asyncio
async def test_memo_outside_window_is_silent(notes_dir, tmp_path, monkeypatch):
    (notes_dir / "メモ.md").write_text("牛乳を買う", "utf-8")
    runner = make_memo_runner(tmp_path, monkeypatch, now=dt.time(12, 0))
    assert await runner._check_memo() is None


@pytest.mark.asyncio
async def test_memo_ignores_old_notes(notes_dir, tmp_path, monkeypatch):
    import os as _os
    import time as _time

    path = notes_dir / "old.md"
    path.write_text("昔のメモ", "utf-8")
    yesterday = _time.time() - 86400
    _os.utime(path, (yesterday, yesterday))
    runner = make_memo_runner(tmp_path, monkeypatch)
    assert await runner._check_memo() is None


@pytest.mark.asyncio
async def test_memo_snippet_clamped(notes_dir, tmp_path, monkeypatch):
    (notes_dir / "long.md").write_text("あ" * 200, "utf-8")
    runner = make_memo_runner(tmp_path, monkeypatch)
    line = await runner._check_memo()
    assert line is not None
    assert "あ" * hb.MEMO_SNIPPET_CHARS in line
    assert "あ" * (hb.MEMO_SNIPPET_CHARS + 1) not in line


def test_memo_snippet_skips_markup_and_blank():
    assert hb._memo_snippet("\n\n# 見出し\n本文") == "見出し"
    assert hb._memo_snippet("- 牛乳を買う") == "牛乳を買う"
    assert hb._memo_snippet("   \n\n") == ""


# ---- Phase E: weather checker ----------------------------------------


def make_weather_runner(tmp_path, monkeypatch, now=dt.time(7, 0)):
    runner = make_runner(
        speak=make_speak(tmp_path, weather_area="270000", weather_city="2720900")
    )
    monkeypatch.setattr(runner, "_now", lambda: now)
    return runner


@pytest.mark.asyncio
async def test_weather_speaks_once_per_day(tmp_path, monkeypatch):
    runner = make_weather_runner(tmp_path, monkeypatch)
    calls = []

    async def fake_check(area, city, threshold, *, today=None):
        calls.append((area, city, threshold))
        return "今日は雨が降りそうだよ"

    monkeypatch.setattr(hb.weather, "check_weather", fake_check)
    line = await runner._check_weather()
    assert line == "今日は雨が降りそうだよ"
    assert calls == [("270000", "2720900", 50)]
    # Daily flag set: no second fetch, no second line.
    assert await runner._check_weather() is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_weather_normal_day_marks_done_silently(tmp_path, monkeypatch):
    runner = make_weather_runner(tmp_path, monkeypatch)

    async def fake_check(area, city, threshold, *, today=None):
        return None

    monkeypatch.setattr(hb.weather, "check_weather", fake_check)
    assert await runner._check_weather() is None
    assert runner._state.get("weather_done") == dt.date.today().isoformat()


@pytest.mark.asyncio
async def test_weather_fetch_failure_retries(tmp_path, monkeypatch):
    runner = make_weather_runner(tmp_path, monkeypatch)

    async def fail(area, city, threshold, *, today=None):
        raise RuntimeError("network down")

    monkeypatch.setattr(hb.weather, "check_weather", fail)
    assert await runner._check_weather() is None
    # Flag NOT set: the next tick inside the window retries.
    assert "weather_done" not in runner._state


@pytest.mark.asyncio
async def test_weather_outside_window_no_fetch(tmp_path, monkeypatch):
    runner = make_weather_runner(tmp_path, monkeypatch, now=dt.time(12, 0))

    async def boom(*a, **k):
        raise AssertionError("must not fetch outside the window")

    monkeypatch.setattr(hb.weather, "check_weather", boom)
    assert await runner._check_weather() is None


# ---- Phase E: tick + speech ------------------------------------------


@pytest.mark.asyncio
async def test_tick_speak_disabled_without_config():
    runner = make_runner()  # speak=None
    assert await runner._tick_speak() is False


@pytest.mark.asyncio
async def test_perform_speak_counts_and_returns_to_idle(tmp_path, monkeypatch):
    gw = FakeGateway()
    runner = make_runner(gw, speak=make_speak(tmp_path))
    spoken = []

    async def fake_synth(arguments, *, gateway=None, registry=None):
        spoken.append(arguments["text"])
        return {"ok": True}

    monkeypatch.setattr(
        "stackchan_mcp.tts.orchestrator.synthesize_and_send", fake_synth
    )
    await runner._perform_speak("テスト発話")
    assert spoken == ["テスト発話"]
    faces = [a["face"] for n, a in gw.esp32.calls if n == "self.display.set_avatar"]
    assert faces == ["happy", "idle"]
    assert runner._spoken_today() == 1
    # Persisted: a restarted runner sees the same count.
    fresh = make_runner(FakeGateway(), speak=make_speak(tmp_path))
    assert fresh._spoken_today() == 1


@pytest.mark.asyncio
async def test_tick_speak_suppressed_then_gesture_fallback(tmp_path, monkeypatch):
    gw = FakeGateway()
    runner = make_runner(gw, speak=make_speak(tmp_path))
    monkeypatch.setattr(hb, "is_recording", lambda: True)
    assert await runner._tick_speak() is False
