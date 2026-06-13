"""Tests for the Phase F device control logic (stackchan_mcp.control)."""

from __future__ import annotations

import json

import pytest

from stackchan_mcp import control


class FakeESP32:
    def __init__(self, *, connected: bool = True, fail: bool = False) -> None:
        self.device_connected = connected
        self.fail = fail
        self.calls: list[tuple[str, dict]] = []
        self.listen_calls: list[tuple[str, str]] = []

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        if self.fail:
            return None, {"code": -32000, "message": "boom"}
        return {"content": [{"type": "text", "text": json.dumps({"ok": True})}]}, None

    async def send_listen_state(self, state: str, mode: str = "manual") -> None:
        self.listen_calls.append((state, mode))


class FakeGateway:
    def __init__(self, *, connected: bool = True, fail: bool = False) -> None:
        self.esp32 = FakeESP32(connected=connected, fail=fail)


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    """Point the control state file at a tmp path for every test."""
    monkeypatch.setenv(
        "STACKCHAN_CONTROL_STATE", str(tmp_path / "control_state.json")
    )


# ---- state persistence ------------------------------------------------


def test_load_state_defaults_when_missing():
    state = control.load_state()
    assert state == {
        "volume": control.DEFAULT_VOLUME,
        "muted": False,
        "pre_mute_volume": control.DEFAULT_VOLUME,
        "mic_gain": control.DEFAULT_MIC_GAIN,
    }


def test_save_then_load_roundtrip(monkeypatch, tmp_path):
    path = tmp_path / "rt.json"
    monkeypatch.setenv("STACKCHAN_CONTROL_STATE", str(path))
    control.save_state(
        {"volume": 33, "muted": True, "pre_mute_volume": 70, "mic_gain": 18}
    )
    assert path.exists()
    loaded = control.load_state()
    assert loaded == {
        "volume": 33,
        "muted": True,
        "pre_mute_volume": 70,
        "mic_gain": 18,
    }


def test_save_state_clamps_out_of_range(monkeypatch, tmp_path):
    path = tmp_path / "clamp.json"
    monkeypatch.setenv("STACKCHAN_CONTROL_STATE", str(path))
    control.save_state({"volume": 999, "muted": False, "pre_mute_volume": -5})
    loaded = control.load_state()
    assert loaded["volume"] == 100
    assert loaded["pre_mute_volume"] == 0


def test_load_state_corrupt_file_uses_defaults(monkeypatch, tmp_path):
    path = tmp_path / "corrupt.json"
    path.write_text("not json", encoding="utf-8")
    monkeypatch.setenv("STACKCHAN_CONTROL_STATE", str(path))
    state = control.load_state()
    assert state["volume"] == control.DEFAULT_VOLUME


# ---- set_volume -------------------------------------------------------


@pytest.mark.asyncio
async def test_set_volume_sends_and_persists():
    gw = FakeGateway()
    result = await control.set_volume(gw, 80)
    assert result == {"ok": True, "volume": 80, "muted": False}
    assert gw.esp32.calls == [("self.audio_speaker.set_volume", {"volume": 80})]
    assert control.load_state()["volume"] == 80


@pytest.mark.asyncio
async def test_set_volume_clears_mute():
    gw = FakeGateway()
    control.save_state({"volume": 0, "muted": True, "pre_mute_volume": 60})
    result = await control.set_volume(gw, 40)
    assert result["muted"] is False
    state = control.load_state()
    assert state["muted"] is False
    assert state["volume"] == 40
    assert state["pre_mute_volume"] == 40


@pytest.mark.asyncio
async def test_set_volume_clamps():
    gw = FakeGateway()
    result = await control.set_volume(gw, 150)
    assert result["volume"] == 100


@pytest.mark.asyncio
async def test_set_volume_device_failure_does_not_persist():
    gw = FakeGateway(fail=True)
    result = await control.set_volume(gw, 80)
    assert result == {"ok": False, "error": "device call failed"}
    # State unchanged (defaults).
    assert control.load_state()["volume"] == control.DEFAULT_VOLUME


# ---- mute / unmute ----------------------------------------------------


@pytest.mark.asyncio
async def test_mute_stashes_volume_and_sets_zero():
    gw = FakeGateway()
    await control.set_volume(gw, 65)
    gw.esp32.calls.clear()
    result = await control.mute(gw)
    assert result == {"ok": True, "volume": 0, "muted": True}
    assert gw.esp32.calls == [("self.audio_speaker.set_volume", {"volume": 0})]
    state = control.load_state()
    assert state["volume"] == 0
    assert state["muted"] is True
    assert state["pre_mute_volume"] == 65


@pytest.mark.asyncio
async def test_unmute_restores_stashed_volume():
    gw = FakeGateway()
    await control.set_volume(gw, 65)
    await control.mute(gw)
    gw.esp32.calls.clear()
    result = await control.unmute(gw)
    assert result == {"ok": True, "volume": 65, "muted": False}
    assert gw.esp32.calls == [("self.audio_speaker.set_volume", {"volume": 65})]
    state = control.load_state()
    assert state["muted"] is False
    assert state["volume"] == 65


@pytest.mark.asyncio
async def test_mute_twice_keeps_original_pre_mute_volume():
    gw = FakeGateway()
    await control.set_volume(gw, 70)
    await control.mute(gw)
    # A second mute must not overwrite pre_mute_volume with 0.
    await control.mute(gw)
    assert control.load_state()["pre_mute_volume"] == 70


# ---- apply_persisted_volume ------------------------------------------


@pytest.mark.asyncio
async def test_apply_persisted_volume_reapplies(monkeypatch):
    monkeypatch.setattr(control, "_APPLY_VOLUME_DELAY_S", 0)
    gw = FakeGateway()
    control.save_state({"volume": 42, "muted": False, "pre_mute_volume": 42})
    await control.apply_persisted_volume(gw)
    assert ("self.audio_speaker.set_volume", {"volume": 42}) in gw.esp32.calls


@pytest.mark.asyncio
async def test_apply_persisted_volume_muted_applies_zero(monkeypatch):
    monkeypatch.setattr(control, "_APPLY_VOLUME_DELAY_S", 0)
    gw = FakeGateway()
    control.save_state({"volume": 0, "muted": True, "pre_mute_volume": 55})
    await control.apply_persisted_volume(gw)
    assert ("self.audio_speaker.set_volume", {"volume": 0}) in gw.esp32.calls


@pytest.mark.asyncio
async def test_apply_persisted_volume_retries_on_failure(monkeypatch):
    monkeypatch.setattr(control, "_APPLY_VOLUME_DELAY_S", 0)
    gw = FakeGateway(fail=True)
    control.save_state({"volume": 42, "muted": False, "pre_mute_volume": 42})
    await control.apply_persisted_volume(gw)
    # Initial attempt + one retry = 2 calls.
    assert len(gw.esp32.calls) == control._APPLY_VOLUME_RETRIES + 1


@pytest.mark.asyncio
async def test_apply_persisted_volume_skips_when_disconnected(monkeypatch):
    monkeypatch.setattr(control, "_APPLY_VOLUME_DELAY_S", 0)
    gw = FakeGateway(connected=False)
    await control.apply_persisted_volume(gw)
    assert gw.esp32.calls == []


# ---- set_device_status_text ------------------------------------------


@pytest.mark.asyncio
async def test_set_status_text_sends_when_connected():
    gw = FakeGateway()
    await control.set_device_status_text(gw, "考え中")
    assert gw.esp32.calls == [
        ("self.display.set_status_text", {"text": "考え中"})
    ]


@pytest.mark.asyncio
async def test_set_status_text_noop_when_disconnected():
    gw = FakeGateway(connected=False)
    await control.set_device_status_text(gw, "考え中")
    assert gw.esp32.calls == []


@pytest.mark.asyncio
async def test_set_status_text_swallows_device_error():
    gw = FakeGateway(fail=True)
    # Must not raise even when the device tool errors (old firmware).
    await control.set_device_status_text(gw, "x")
    assert gw.esp32.calls  # it tried


@pytest.mark.asyncio
async def test_set_status_text_swallows_exception(monkeypatch):
    gw = FakeGateway()

    async def boom(name, args):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(gw.esp32, "call_tool", boom)
    # Must not propagate — voice turns rely on this never raising.
    await control.set_device_status_text(gw, "x")


# ---- trigger_listen ---------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_listen_sends_start(monkeypatch):
    import stackchan_mcp.audio_stream as audio_stream

    monkeypatch.setattr(audio_stream, "is_recording", lambda: False)
    gw = FakeGateway()
    result = await control.trigger_listen(gw)
    assert result == {"ok": True}
    assert gw.esp32.listen_calls == [("start", "manual")]


@pytest.mark.asyncio
async def test_trigger_listen_already_recording(monkeypatch):
    import stackchan_mcp.audio_stream as audio_stream

    monkeypatch.setattr(audio_stream, "is_recording", lambda: True)
    gw = FakeGateway()
    result = await control.trigger_listen(gw)
    assert result == {"ok": False, "error": "already listening"}
    assert gw.esp32.listen_calls == []


@pytest.mark.asyncio
async def test_trigger_listen_no_device():
    gw = FakeGateway(connected=False)
    result = await control.trigger_listen(gw)
    assert result == {"ok": False, "error": "no device connected"}


# ---- Phase F extras: subtitle / route badge / LED indicator ----------


@pytest.mark.asyncio
async def test_set_subtitle_sends_when_connected():
    gw = FakeGateway()
    await control.set_device_subtitle(gw, "こんにちは")
    assert gw.esp32.calls == [
        ("self.display.set_subtitle", {"text": "こんにちは"})
    ]


@pytest.mark.asyncio
async def test_set_subtitle_noop_when_disconnected():
    gw = FakeGateway(connected=False)
    await control.set_device_subtitle(gw, "x")
    assert gw.esp32.calls == []


@pytest.mark.asyncio
async def test_set_subtitle_swallows_device_error():
    gw = FakeGateway(fail=True)
    await control.set_device_subtitle(gw, "x")  # must not raise
    assert gw.esp32.calls  # it tried


@pytest.mark.asyncio
async def test_set_route_badge_sends():
    gw = FakeGateway()
    await control.set_device_route_badge(gw, "H")
    assert gw.esp32.calls == [("self.display.set_route_badge", {"text": "H"})]


@pytest.mark.asyncio
async def test_set_route_badge_noop_when_disconnected():
    gw = FakeGateway(connected=False)
    await control.set_device_route_badge(gw, "H")
    assert gw.esp32.calls == []


@pytest.mark.asyncio
async def test_set_led_indicator_sends_rgb():
    gw = FakeGateway()
    await control.set_device_led_indicator(gw, 0, 0, 32)
    assert gw.esp32.calls == [
        ("self.led.set_indicator", {"r": 0, "g": 0, "b": 32})
    ]


@pytest.mark.asyncio
async def test_set_led_indicator_clear_is_zero():
    gw = FakeGateway()
    await control.set_device_led_indicator(gw, 0, 0, 0)
    assert gw.esp32.calls == [
        ("self.led.set_indicator", {"r": 0, "g": 0, "b": 0})
    ]


@pytest.mark.asyncio
async def test_set_led_indicator_swallows_exception(monkeypatch):
    gw = FakeGateway()

    async def boom(name, args):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(gw.esp32, "call_tool", boom)
    await control.set_device_led_indicator(gw, 0, 0, 32)  # must not raise


# ---- mic gain ---------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_conversation():
    """Reset the volatile conversation ring between tests."""
    control._CONVERSATION.clear()
    yield
    control._CONVERSATION.clear()


@pytest.mark.asyncio
async def test_set_mic_gain_sends_and_persists():
    gw = FakeGateway()
    result = await control.set_mic_gain(gw, 24)
    assert result == {"ok": True, "gain": 24}
    assert gw.esp32.calls == [("self.audio_speaker.set_mic_gain", {"gain": 24})]
    assert control.load_state()["mic_gain"] == 24


@pytest.mark.asyncio
async def test_set_mic_gain_clamps_high():
    gw = FakeGateway()
    result = await control.set_mic_gain(gw, 999)
    assert result == {"ok": True, "gain": 36}
    assert gw.esp32.calls == [("self.audio_speaker.set_mic_gain", {"gain": 36})]
    assert control.load_state()["mic_gain"] == 36


@pytest.mark.asyncio
async def test_set_mic_gain_clamps_low():
    gw = FakeGateway()
    result = await control.set_mic_gain(gw, -5)
    assert result == {"ok": True, "gain": 0}
    assert control.load_state()["mic_gain"] == 0


@pytest.mark.asyncio
async def test_set_mic_gain_device_failure_does_not_persist():
    gw = FakeGateway(fail=True)
    result = await control.set_mic_gain(gw, 20)
    assert result == {"ok": False, "error": "device call failed"}
    assert control.load_state()["mic_gain"] == control.DEFAULT_MIC_GAIN


@pytest.mark.asyncio
async def test_set_mic_gain_preserves_volume_state():
    gw = FakeGateway()
    await control.set_volume(gw, 80)
    await control.set_mic_gain(gw, 12)
    state = control.load_state()
    assert state["volume"] == 80
    assert state["mic_gain"] == 12


@pytest.mark.asyncio
async def test_apply_persisted_mic_gain_reapplies(monkeypatch):
    monkeypatch.setattr(control, "_APPLY_VOLUME_DELAY_S", 0)
    gw = FakeGateway()
    control.save_state(
        {"volume": 50, "muted": False, "pre_mute_volume": 50, "mic_gain": 22}
    )
    await control.apply_persisted_mic_gain(gw)
    assert ("self.audio_speaker.set_mic_gain", {"gain": 22}) in gw.esp32.calls


@pytest.mark.asyncio
async def test_apply_persisted_mic_gain_retries_on_failure(monkeypatch):
    monkeypatch.setattr(control, "_APPLY_VOLUME_DELAY_S", 0)
    gw = FakeGateway(fail=True)
    control.save_state(
        {"volume": 50, "muted": False, "pre_mute_volume": 50, "mic_gain": 22}
    )
    await control.apply_persisted_mic_gain(gw)
    assert len(gw.esp32.calls) == control._APPLY_VOLUME_RETRIES + 1


@pytest.mark.asyncio
async def test_apply_persisted_mic_gain_skips_when_disconnected(monkeypatch):
    monkeypatch.setattr(control, "_APPLY_VOLUME_DELAY_S", 0)
    gw = FakeGateway(connected=False)
    await control.apply_persisted_mic_gain(gw)
    assert gw.esp32.calls == []


# ---- conversation log -------------------------------------------------


def test_conversation_empty_by_default():
    assert control.get_conversation() == {"ok": True, "turns": []}


def test_record_conversation_turn_appends():
    control.record_conversation_turn("こんにちは", "やあ", "local", {"total": 500})
    result = control.get_conversation()
    assert result["ok"] is True
    assert len(result["turns"]) == 1
    turn = result["turns"][0]
    assert turn["transcript"] == "こんにちは"
    assert turn["reply"] == "やあ"
    assert turn["route"] == "local"
    assert turn["timings_ms"] == {"total": 500}
    assert isinstance(turn["ts"], float)


def test_record_conversation_turn_timings_optional():
    control.record_conversation_turn("test", "reply", "hermes")
    turn = control.get_conversation()["turns"][0]
    assert turn["timings_ms"] is None


def test_conversation_is_oldest_first():
    control.record_conversation_turn("first", "r1", "local")
    control.record_conversation_turn("second", "r2", "hermes")
    transcripts = [t["transcript"] for t in control.get_conversation()["turns"]]
    assert transcripts == ["first", "second"]


def test_conversation_ring_drops_oldest_past_maxlen():
    maxlen = control._CONVERSATION.maxlen
    assert maxlen == 30
    for i in range(maxlen + 5):
        control.record_conversation_turn(f"t{i}", f"r{i}", "local")
    turns = control.get_conversation()["turns"]
    assert len(turns) == maxlen
    # Oldest five dropped; first surviving turn is t5.
    assert turns[0]["transcript"] == "t5"
    assert turns[-1]["transcript"] == f"t{maxlen + 4}"


# ---- get_audio_level --------------------------------------------------


def test_get_audio_level_idle_when_not_recording(monkeypatch):
    import stackchan_mcp.audio_stream as audio_stream

    monkeypatch.setattr(audio_stream, "is_recording", lambda: False)
    monkeypatch.setattr(audio_stream, "get_input_level", lambda: 0.7)
    result = control.get_audio_level()
    # Not recording → level forced to 0.0 regardless of the stale value.
    assert result == {"ok": True, "recording": False, "level": 0.0}


def test_get_audio_level_reports_level_when_recording(monkeypatch):
    import stackchan_mcp.audio_stream as audio_stream

    monkeypatch.setattr(audio_stream, "is_recording", lambda: True)
    monkeypatch.setattr(audio_stream, "get_input_level", lambda: 0.42)
    result = control.get_audio_level()
    assert result == {"ok": True, "recording": True, "level": 0.42}
