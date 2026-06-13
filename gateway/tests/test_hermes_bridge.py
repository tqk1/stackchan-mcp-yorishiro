"""Tests for the Hermes voice bridge (ask_hermes request shape)."""

from typing import Any
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from stackchan_mcp import control, hermes_bridge
from stackchan_mcp.capture_server import GATEWAY_KEY
from stackchan_mcp.hermes_bridge import (
    DEFAULT_VOICE_SYSTEM_PROMPT,
    HERMES_VOICE_TOOLS_LINE,
    ask_hermes,
)


@pytest.fixture
def aiohttp_unused_port():
    """Helper: pick an unused TCP port via ephemeral bind."""
    import socket

    def _pick() -> int:
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]
        finally:
            sock.close()

    return _pick


async def _run_hermes_stub(handler, aiohttp_unused_port):
    app = web.Application()
    app.router.add_route("POST", "/v1/chat/completions", handler)
    port = aiohttp_unused_port()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner, f"http://127.0.0.1:{port}"


@pytest.mark.asyncio
async def test_ask_hermes_system_prompt_carries_tool_guidance(
    monkeypatch, aiohttp_unused_port
):
    """The system message must combine the voice style prompt with the
    MCP tool-routing guidance — without the latter the agent drifts to
    its approval-gated built-in tools or fakes completions (observed
    live in the Phase D2 E2E)."""
    received: dict[str, Any] = {}

    async def handle(request: web.Request) -> web.Response:
        received["payload"] = await request.json()
        return web.json_response(
            {"choices": [{"message": {"role": "assistant", "content": "はい "}}]}
        )

    runner, base_url = await _run_hermes_stub(handle, aiohttp_unused_port)
    monkeypatch.setenv("HERMES_API_URL", base_url)
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    monkeypatch.delenv("HERMES_VOICE_SYSTEM_PROMPT", raising=False)
    try:
        reply = await ask_hermes("メモして")
    finally:
        await runner.cleanup()

    assert reply == "はい"
    system = received["payload"]["messages"][0]
    assert system["role"] == "system"
    assert system["content"].startswith(DEFAULT_VOICE_SYSTEM_PROMPT)
    assert HERMES_VOICE_TOOLS_LINE in system["content"]
    assert received["payload"]["messages"][1] == {
        "role": "user",
        "content": "メモして",
    }


@pytest.mark.asyncio
async def test_ask_hermes_custom_prompt_still_gets_tool_guidance(
    monkeypatch, aiohttp_unused_port
):
    received: dict[str, Any] = {}

    async def handle(request: web.Request) -> web.Response:
        received["payload"] = await request.json()
        return web.json_response(
            {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

    runner, base_url = await _run_hermes_stub(handle, aiohttp_unused_port)
    monkeypatch.setenv("HERMES_API_URL", base_url)
    monkeypatch.setenv("HERMES_VOICE_SYSTEM_PROMPT", "カスタム。")
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    try:
        await ask_hermes("こんにちは")
    finally:
        await runner.cleanup()

    system = received["payload"]["messages"][0]
    assert system["content"].startswith("カスタム。")
    assert HERMES_VOICE_TOOLS_LINE in system["content"]


@pytest.mark.asyncio
async def test_ask_hermes_error_status_raises(monkeypatch, aiohttp_unused_port):
    async def handle(request: web.Request) -> web.Response:
        return web.Response(status=500, text="boom")

    runner, base_url = await _run_hermes_stub(handle, aiohttp_unused_port)
    monkeypatch.setenv("HERMES_API_URL", base_url)
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    try:
        with pytest.raises(RuntimeError, match="status=500"):
            await ask_hermes("こんにちは")
    finally:
        await runner.cleanup()


# ---- Phase F: voice-turn status-text feedback ------------------------


class _StubESP32:
    def __init__(self) -> None:
        self.device_connected = True


class _StubGateway:
    def __init__(self) -> None:
        self.esp32 = _StubESP32()
        self.voice_turn_active = False
        self._interactions = 0

    def note_human_interaction(self) -> None:
        self._interactions += 1


class _StubEngine:
    def __init__(self, text: str) -> None:
        self._text = text

    async def transcribe(self, pcm, language="ja"):
        return {"text": self._text}


def _make_voice_request(gateway) -> web.Request:
    from aiohttp import StreamReader

    app = web.Application()
    app[GATEWAY_KEY] = gateway
    # A real StreamReader so request.content.read() works; make_mocked_request
    # otherwise leaves content unset / as a bytes placeholder.
    body = b"oggdata"
    reader = StreamReader(protocol=mock.Mock(_reading_paused=False), limit=2**16)
    reader.feed_data(body)
    reader.feed_eof()
    # Authorise via the shared hook token (set by the tests) rather than
    # the loopback fallback, which depends on a transport peername that
    # make_mocked_request does not populate in this aiohttp version.
    return make_mocked_request(
        "POST",
        "/voice_turn",
        headers={
            "X-StackChan-Session": "sess-1",
            "Authorization": "Bearer turn-token",
            "Content-Length": str(len(body)),
        },
        payload=reader,
        app=app,
    )


def _patch_voice_pipeline(monkeypatch, *, transcript: str, reply: str = "はい"):
    """Stub decode / STT / brain / TTS so the turn runs without deps."""
    import stackchan_mcp.stt as stt_mod
    import stackchan_mcp.tts.orchestrator as tts_orch

    monkeypatch.setattr(
        hermes_bridge, "_ogg_opus_to_pcm16k", lambda data: b"\x00\x00"
    )

    class _Registry:
        def get(self, name):
            return _StubEngine(transcript)

    monkeypatch.setattr(stt_mod, "get_registry", lambda: _Registry())

    async def fake_generate_reply(text):
        return reply, "hermes"

    monkeypatch.setattr(hermes_bridge, "generate_reply", fake_generate_reply)

    async def fake_send(arguments, *, gateway=None, **kw):
        return {"frame_count": 1}

    monkeypatch.setattr(tts_orch, "synthesize_and_send", fake_send)


def _record_status_text(monkeypatch) -> list[str]:
    seen: list[str] = []

    async def fake_status(gateway, text):
        seen.append(text)

    monkeypatch.setattr(control, "set_device_status_text", fake_status)
    return seen


@pytest.mark.asyncio
async def test_voice_turn_status_text_sequence(monkeypatch):
    monkeypatch.setenv("STACKCHAN_AUDIO_HOOK_TOKEN", "turn-token")
    seen = _record_status_text(monkeypatch)
    _patch_voice_pipeline(monkeypatch, transcript="おはよう", reply="やあ")
    gateway = _StubGateway()
    request = _make_voice_request(gateway)

    response = await hermes_bridge.handle_voice_turn(request)

    assert response.status == 200
    # きいてるよ (STT) → 考え中 (brain) → "" (clear in finally).
    assert seen == [
        control.STATUS_LISTENING,
        control.STATUS_THINKING,
        control.STATUS_CLEAR,
    ]
    assert gateway.voice_turn_active is False
    assert gateway._interactions == 1


@pytest.mark.asyncio
async def test_voice_turn_clears_status_on_empty_transcript(monkeypatch):
    monkeypatch.setenv("STACKCHAN_AUDIO_HOOK_TOKEN", "turn-token")
    seen = _record_status_text(monkeypatch)
    _patch_voice_pipeline(monkeypatch, transcript="   ")
    gateway = _StubGateway()
    request = _make_voice_request(gateway)

    response = await hermes_bridge.handle_voice_turn(request)

    assert response.status == 200
    # Listening shown, then cleared in finally (no 考え中 — empty STT).
    assert seen[0] == control.STATUS_LISTENING
    assert seen[-1] == control.STATUS_CLEAR
    assert control.STATUS_THINKING not in seen
    assert gateway.voice_turn_active is False


@pytest.mark.asyncio
async def test_voice_turn_clears_status_when_brain_fails(monkeypatch):
    monkeypatch.setenv("STACKCHAN_AUDIO_HOOK_TOKEN", "turn-token")
    seen = _record_status_text(monkeypatch)
    _patch_voice_pipeline(monkeypatch, transcript="天気は")

    async def boom(text):
        raise RuntimeError("hermes down")

    monkeypatch.setattr(hermes_bridge, "generate_reply", boom)
    gateway = _StubGateway()
    request = _make_voice_request(gateway)

    response = await hermes_bridge.handle_voice_turn(request)

    assert response.status == 502
    assert seen[-1] == control.STATUS_CLEAR
    assert gateway.voice_turn_active is False
