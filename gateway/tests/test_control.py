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
        "brightness": control.DEFAULT_BRIGHTNESS,
        "led": control.DEFAULT_LED,
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
        "brightness": control.DEFAULT_BRIGHTNESS,
        "led": control.DEFAULT_LED,
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


@pytest.mark.asyncio
async def test_concurrent_mute_unmute_preserve_pre_mute_volume():
    # mute/unmute do a read-modify-write under _mute_lock; firing them
    # concurrently must not stash 0 into pre_mute_volume and lose the
    # real level. Whichever wins, the stashed volume stays 75.
    gw = FakeGateway()
    await control.set_volume(gw, 75)
    import asyncio

    await asyncio.gather(control.mute(gw), control.unmute(gw))
    assert control.load_state()["pre_mute_volume"] == 75


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


# ---- brightness -------------------------------------------------------


@pytest.mark.asyncio
async def test_set_brightness_sends_and_persists():
    gw = FakeGateway()
    result = await control.set_brightness(gw, 40)
    assert result == {"ok": True, "brightness": 40}
    assert gw.esp32.calls == [("self.screen.set_brightness", {"brightness": 40})]
    assert control.load_state()["brightness"] == 40


@pytest.mark.asyncio
async def test_set_brightness_clamps_out_of_range():
    gw = FakeGateway()
    assert (await control.set_brightness(gw, 999))["brightness"] == 100
    assert (await control.set_brightness(gw, -5))["brightness"] == 0
    assert control.load_state()["brightness"] == 0


@pytest.mark.asyncio
async def test_set_brightness_device_failure_does_not_persist():
    gw = FakeGateway(fail=True)
    result = await control.set_brightness(gw, 20)
    assert result == {"ok": False, "error": "device call failed"}
    assert control.load_state()["brightness"] == control.DEFAULT_BRIGHTNESS


@pytest.mark.asyncio
async def test_apply_persisted_brightness_reapplies(monkeypatch):
    monkeypatch.setattr(control, "_APPLY_VOLUME_DELAY_S", 0)
    gw = FakeGateway()
    control.save_state({"brightness": 33})
    await control.apply_persisted_brightness(gw)
    assert ("self.screen.set_brightness", {"brightness": 33}) in gw.esp32.calls


@pytest.mark.asyncio
async def test_apply_persisted_brightness_skips_when_disconnected(monkeypatch):
    monkeypatch.setattr(control, "_APPLY_VOLUME_DELAY_S", 0)
    gw = FakeGateway(connected=False)
    await control.apply_persisted_brightness(gw)
    assert gw.esp32.calls == []


# ---- LED (3 slots: idle / listening / hermes) ------------------------


def test_led_default_is_three_slots():
    led = control.load_state()["led"]
    assert led == {
        "brightness": 100,
        "idle": {"on": False, "r": 30, "g": 144, "b": 255},
        "listening": {"r": 0, "g": 210, "b": 90},
        "hermes": {"r": 148, "g": 108, "b": 255},
    }


def test_led_backward_compat_flat_migrates_to_idle(monkeypatch, tmp_path):
    path = tmp_path / "old.json"
    monkeypatch.setenv("STACKCHAN_CONTROL_STATE", str(path))
    # The pre-3-slot on-disk shape was a flat {on, r, g, b}.
    path.write_text(
        json.dumps({"led": {"on": True, "r": 1, "g": 2, "b": 3}}), encoding="utf-8"
    )
    led = control.load_state()["led"]
    assert led["idle"] == {"on": True, "r": 1, "g": 2, "b": 3}
    assert led["listening"] == control.DEFAULT_LED["listening"]
    assert led["hermes"] == control.DEFAULT_LED["hermes"]


@pytest.mark.asyncio
async def test_set_led_idle_on_uses_set_all_and_persists():
    gw = FakeGateway()
    result = await control.set_led(gw, "idle", on=True, r=10, g=20, b=30)
    assert result["ok"] is True
    assert result["led"]["idle"] == {"on": True, "r": 10, "g": 20, "b": 30}
    assert gw.esp32.calls == [("self.led.set_all", {"r": 10, "g": 20, "b": 30})]
    assert control.load_state()["led"]["idle"] == {"on": True, "r": 10, "g": 20, "b": 30}


@pytest.mark.asyncio
async def test_set_led_idle_off_clears_but_keeps_colour():
    gw = FakeGateway()
    result = await control.set_led(gw, "idle", on=False, r=10, g=20, b=30)
    assert result["led"]["idle"] == {"on": False, "r": 10, "g": 20, "b": 30}
    assert gw.esp32.calls == [("self.led.clear", {})]


@pytest.mark.asyncio
async def test_set_led_idle_skips_device_when_voice_turn_active():
    gw = FakeGateway()
    gw.voice_turn_active = True  # a turn owns the LED right now
    result = await control.set_led(gw, "idle", on=True, r=1, g=2, b=3)
    assert result["ok"] is True
    assert gw.esp32.calls == []  # persisted only; restore picks it up
    assert control.load_state()["led"]["idle"]["on"] is True


@pytest.mark.asyncio
async def test_set_led_listening_persists_without_device_call():
    gw = FakeGateway()
    result = await control.set_led(gw, "listening", r=5, g=6, b=7)
    assert result["led"]["listening"] == {"r": 5, "g": 6, "b": 7}
    assert gw.esp32.calls == []  # shown during the phase / via preview
    assert control.load_state()["led"]["listening"] == {"r": 5, "g": 6, "b": 7}


@pytest.mark.asyncio
async def test_set_led_rejects_unknown_slot():
    gw = FakeGateway()
    result = await control.set_led(gw, "nope", on=True, r=1, g=2, b=3)
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_set_led_clamps_rgb():
    gw = FakeGateway()
    result = await control.set_led(gw, "hermes", r=999, g=-5, b=256)
    assert result["led"]["hermes"] == {"r": 255, "g": 0, "b": 255}


@pytest.mark.asyncio
async def test_set_led_idle_device_failure_does_not_persist():
    gw = FakeGateway(fail=True)
    result = await control.set_led(gw, "idle", on=True, r=10, g=20, b=30)
    assert result == {"ok": False, "error": "device call failed"}
    assert control.load_state()["led"] == control.DEFAULT_LED


@pytest.mark.asyncio
async def test_apply_led_state_idle_on_lights_colour():
    gw = FakeGateway()
    control.save_state({"led": {"idle": {"on": True, "r": 1, "g": 2, "b": 3}}})
    await control.apply_led_state(gw, "idle")
    assert gw.esp32.calls == [("self.led.set_all", {"r": 1, "g": 2, "b": 3})]


@pytest.mark.asyncio
async def test_apply_led_state_idle_off_clears():
    gw = FakeGateway()
    await control.apply_led_state(gw, "idle")  # default idle off
    assert gw.esp32.calls == [("self.led.clear", {})]


@pytest.mark.asyncio
async def test_apply_led_state_listening_lights_colour():
    gw = FakeGateway()
    await control.apply_led_state(gw, "listening")
    assert gw.esp32.calls == [("self.led.set_all", {"r": 0, "g": 210, "b": 90})]


@pytest.mark.asyncio
async def test_set_led_brightness_scales_live_idle_and_persists():
    gw = FakeGateway()
    await control.set_led(gw, "idle", on=True, r=100, g=200, b=50)
    gw.esp32.calls.clear()
    result = await control.set_led_brightness(gw, 50)
    assert result == {"ok": True, "brightness": 50}
    assert control.load_state()["led"]["brightness"] == 50
    # 50% scale applied to the live idle colour
    assert ("self.led.set_all", {"r": 50, "g": 100, "b": 25}) in gw.esp32.calls


@pytest.mark.asyncio
async def test_led_brightness_scales_apply_led_state():
    gw = FakeGateway()
    control.save_state(
        {"led": {"brightness": 50, "listening": {"r": 0, "g": 200, "b": 100}}}
    )
    await control.apply_led_state(gw, "listening")
    assert gw.esp32.calls == [("self.led.set_all", {"r": 0, "g": 100, "b": 50})]


@pytest.mark.asyncio
async def test_set_led_brightness_clamps():
    gw = FakeGateway()
    assert (await control.set_led_brightness(gw, 999))["brightness"] == 100
    assert (await control.set_led_brightness(gw, -5))["brightness"] == 0


@pytest.mark.asyncio
async def test_apply_led_state_swallows_exception(monkeypatch):
    gw = FakeGateway()

    async def boom(*_a, **_k):
        raise RuntimeError("device gone")

    monkeypatch.setattr(gw.esp32, "call_tool", boom)
    await control.apply_led_state(gw, "hermes")  # must not raise


@pytest.mark.asyncio
async def test_apply_persisted_led_reapplies_when_idle_on(monkeypatch):
    monkeypatch.setattr(control, "_APPLY_VOLUME_DELAY_S", 0)
    gw = FakeGateway()
    control.save_state({"led": {"idle": {"on": True, "r": 1, "g": 2, "b": 3}}})
    await control.apply_persisted_led(gw)
    assert ("self.led.set_all", {"r": 1, "g": 2, "b": 3}) in gw.esp32.calls


@pytest.mark.asyncio
async def test_apply_persisted_led_noop_when_idle_off(monkeypatch):
    monkeypatch.setattr(control, "_APPLY_VOLUME_DELAY_S", 0)
    gw = FakeGateway()
    await control.apply_persisted_led(gw)  # default idle off
    assert gw.esp32.calls == []


@pytest.mark.asyncio
async def test_preview_led_flashes_then_reverts_to_idle(monkeypatch):
    monkeypatch.setattr(control, "LED_PREVIEW_SECONDS", 0)
    gw = FakeGateway()
    result = await control.preview_led(gw, "listening")
    assert result == {"ok": True, "slot": "listening"}
    # listening colour shown, then idle (default off) -> clear.
    assert gw.esp32.calls == [
        ("self.led.set_all", {"r": 0, "g": 210, "b": 90}),
        ("self.led.clear", {}),
    ]


@pytest.mark.asyncio
async def test_preview_led_refused_during_voice_turn():
    gw = FakeGateway()
    gw.voice_turn_active = True
    result = await control.preview_led(gw, "hermes")
    assert result["ok"] is False
    assert gw.esp32.calls == []


@pytest.mark.asyncio
async def test_preview_led_no_device():
    gw = FakeGateway(connected=False)
    result = await control.preview_led(gw, "hermes")
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_restore_idle_led_is_apply_idle():
    gw = FakeGateway()
    control.save_state({"led": {"idle": {"on": True, "r": 7, "g": 8, "b": 9}}})
    await control.restore_idle_led(gw)
    assert gw.esp32.calls == [("self.led.set_all", {"r": 7, "g": 8, "b": 9})]


# ---- set_head_angle ---------------------------------------------------


@pytest.mark.asyncio
async def test_set_head_angle_sends_clamped():
    gw = FakeGateway()
    result = await control.set_head_angle(gw, 30, 60)
    assert result == {"ok": True, "yaw": 30, "pitch": 60, "connected": True}
    assert gw.esp32.calls == [
        ("self.robot.set_head_angles", {"yaw": 30, "pitch": 60})
    ]


@pytest.mark.asyncio
async def test_set_head_angle_clamps_out_of_range():
    gw = FakeGateway()
    result = await control.set_head_angle(gw, 200, 1)
    # yaw clamped to 90, pitch floored at 5.
    assert result == {"ok": True, "yaw": 90, "pitch": 5, "connected": True}
    assert gw.esp32.calls == [
        ("self.robot.set_head_angles", {"yaw": 90, "pitch": 5})
    ]


@pytest.mark.asyncio
async def test_set_head_angle_clamps_low_yaw_high_pitch():
    gw = FakeGateway()
    result = await control.set_head_angle(gw, -200, 999)
    assert result == {"ok": True, "yaw": -90, "pitch": 85, "connected": True}


@pytest.mark.asyncio
async def test_set_head_angle_disconnected():
    gw = FakeGateway(connected=False)
    result = await control.set_head_angle(gw, 0, 30)
    assert result == {
        "ok": False,
        "error": "no device connected",
        "yaw": 0,
        "pitch": 30,
        "connected": False,
    }
    assert gw.esp32.calls == []


@pytest.mark.asyncio
async def test_set_head_angle_device_failure():
    gw = FakeGateway(fail=True)
    result = await control.set_head_angle(gw, 10, 40)
    assert result == {
        "ok": False,
        "error": "device call failed",
        "yaw": 10,
        "pitch": 40,
        "connected": True,
    }


# ---- set_neutral_pose -------------------------------------------------


@pytest.mark.asyncio
async def test_set_neutral_pose_sends_clamped():
    gw = FakeGateway()
    result = await control.set_neutral_pose(gw, -20, 50)
    assert result == {"ok": True, "yaw": -20, "pitch": 50, "connected": True}
    assert gw.esp32.calls == [
        ("self.robot.set_neutral_pose", {"yaw": -20, "pitch": 50})
    ]


@pytest.mark.asyncio
async def test_set_neutral_pose_clamps_out_of_range():
    gw = FakeGateway()
    result = await control.set_neutral_pose(gw, 999, 0)
    assert result == {"ok": True, "yaw": 90, "pitch": 5, "connected": True}


@pytest.mark.asyncio
async def test_set_neutral_pose_disconnected():
    gw = FakeGateway(connected=False)
    result = await control.set_neutral_pose(gw, 0, 30)
    assert result == {
        "ok": False,
        "error": "no device connected",
        "yaw": 0,
        "pitch": 30,
        "connected": False,
    }
    assert gw.esp32.calls == []


@pytest.mark.asyncio
async def test_set_neutral_pose_device_failure():
    gw = FakeGateway(fail=True)
    result = await control.set_neutral_pose(gw, 5, 45)
    assert result == {
        "ok": False,
        "error": "device call failed",
        "yaw": 5,
        "pitch": 45,
        "connected": True,
    }


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
