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
    }


def test_save_then_load_roundtrip(monkeypatch, tmp_path):
    path = tmp_path / "rt.json"
    monkeypatch.setenv("STACKCHAN_CONTROL_STATE", str(path))
    control.save_state({"volume": 33, "muted": True, "pre_mute_volume": 70})
    assert path.exists()
    loaded = control.load_state()
    assert loaded == {"volume": 33, "muted": True, "pre_mute_volume": 70}


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
