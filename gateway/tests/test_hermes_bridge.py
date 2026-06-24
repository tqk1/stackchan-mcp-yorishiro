"""Tests for the Hermes voice bridge (ask_hermes request shape)."""

import json
from typing import Any
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from stackchan_mcp import control, hermes_bridge, multiturn
from stackchan_mcp.capture_server import GATEWAY_KEY
from stackchan_mcp.hermes_bridge import (
    DEFAULT_VOICE_SYSTEM_PROMPT,
    HERMES_VOICE_TOOLS_LINE,
    ask_hermes,
)
from stackchan_mcp.multiturn import MultiturnSession


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
async def test_ask_hermes_explicit_system_prompt_overrides_env(
    monkeypatch, aiohttp_unused_port
):
    """The proactive speaker passes its own prompt via system_prompt=; it
    must win over HERMES_VOICE_SYSTEM_PROMPT while the tool line still
    appends (so a proactive turn can still reach the MCP tools)."""
    received: dict[str, Any] = {}

    async def handle(request: web.Request) -> web.Response:
        received["payload"] = await request.json()
        return web.json_response(
            {"choices": [{"message": {"role": "assistant", "content": "おかえり"}}]}
        )

    runner, base_url = await _run_hermes_stub(handle, aiohttp_unused_port)
    monkeypatch.setenv("HERMES_API_URL", base_url)
    monkeypatch.setenv("HERMES_VOICE_SYSTEM_PROMPT", "env-prompt。")
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    try:
        reply = await ask_hermes("状況テスト", system_prompt="自発プロンプト。")
    finally:
        await runner.cleanup()

    assert reply == "おかえり"
    system = received["payload"]["messages"][0]
    assert system["content"].startswith("自発プロンプト。")
    assert "env-prompt。" not in system["content"]
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


# ---- Phase 2: per-conversation Hermes session id ---------------------


@pytest.mark.asyncio
async def test_ask_hermes_sends_conversation_session_id(
    monkeypatch, aiohttp_unused_port
):
    """With the API key set, the supplied per-conversation id is sent as
    X-Hermes-Session-Id (so Hermes keeps context within a conversation)."""
    received: dict[str, Any] = {}

    async def handle(request: web.Request) -> web.Response:
        received["headers"] = dict(request.headers)
        return web.json_response(
            {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

    runner, base_url = await _run_hermes_stub(handle, aiohttp_unused_port)
    monkeypatch.setenv("HERMES_API_URL", base_url)
    monkeypatch.setenv("HERMES_API_KEY", "secret")
    monkeypatch.delenv("HERMES_VOICE_SYSTEM_PROMPT", raising=False)
    try:
        await ask_hermes("やあ", session_id="stackchan-voice-abc123")
    finally:
        await runner.cleanup()

    assert received["headers"]["X-Hermes-Session-Id"] == "stackchan-voice-abc123"
    assert received["headers"]["Authorization"] == "Bearer secret"


@pytest.mark.asyncio
async def test_ask_hermes_session_id_falls_back_to_env(
    monkeypatch, aiohttp_unused_port
):
    """Without a per-conversation id (other callers / tests) the fixed
    HERMES_SESSION_ID is used — the pre-Phase-2 behaviour."""
    received: dict[str, Any] = {}

    async def handle(request: web.Request) -> web.Response:
        received["headers"] = dict(request.headers)
        return web.json_response(
            {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

    runner, base_url = await _run_hermes_stub(handle, aiohttp_unused_port)
    monkeypatch.setenv("HERMES_API_URL", base_url)
    monkeypatch.setenv("HERMES_API_KEY", "secret")
    monkeypatch.setenv("HERMES_SESSION_ID", "fixed-id")
    try:
        await ask_hermes("やあ")
    finally:
        await runner.cleanup()

    assert received["headers"]["X-Hermes-Session-Id"] == "fixed-id"


@pytest.mark.asyncio
async def test_ask_hermes_no_session_header_without_key(
    monkeypatch, aiohttp_unused_port
):
    """Session continuity is gated on the API key; without it no session
    header leaks even when a conversation id is supplied."""
    received: dict[str, Any] = {}

    async def handle(request: web.Request) -> web.Response:
        received["headers"] = dict(request.headers)
        return web.json_response(
            {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

    runner, base_url = await _run_hermes_stub(handle, aiohttp_unused_port)
    monkeypatch.setenv("HERMES_API_URL", base_url)
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    try:
        await ask_hermes("やあ", session_id="stackchan-voice-abc123")
    finally:
        await runner.cleanup()

    assert "X-Hermes-Session-Id" not in received["headers"]


# ---- Phase F: voice-turn status-text feedback ------------------------


class _StubESP32:
    def __init__(self) -> None:
        self.device_connected = True
        self.listen_calls: list[tuple[str, str]] = []

    async def send_listen_state(self, state: str, mode: str = "manual") -> None:
        self.listen_calls.append((state, mode))


class _StubGateway:
    def __init__(self) -> None:
        self.esp32 = _StubESP32()
        self.voice_turn_active = False
        self.multiturn = MultiturnSession()
        self.multiturn_active = False
        self.multiturn_prompt_pending = False
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


def _patch_voice_pipeline(
    monkeypatch, *, transcript: str, reply: str = "はい", route: str = "hermes"
):
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

    async def fake_generate_reply(text, *, force_hermes=False, session_id=None):
        return reply, route

    monkeypatch.setattr(hermes_bridge, "generate_reply", fake_generate_reply)
    # The turn reads the persisted Hermes-pin flag; default it off so
    # tests never touch the real ~/.stackchan control state.
    monkeypatch.setattr(control, "routing_force_hermes", lambda: False)
    # The multi-turn gate likewise reads persisted state; bind it to the
    # env reader so tests gate purely via STACKCHAN_MULTITURN and never
    # depend on (or get perturbed by) a live dashboard toggle in the real
    # control state file (mirrors the routing_force_hermes stub above).
    monkeypatch.setattr(control, "multiturn_enabled", multiturn.is_enabled)

    async def fake_send(arguments, *, gateway=None, **kw):
        return {"frame_count": 1}

    monkeypatch.setattr(tts_orch, "synthesize_and_send", fake_send)


def _record_device_cosmetics(monkeypatch) -> dict[str, list]:
    """Record subtitle / route-badge / LED calls in invocation order."""
    rec: dict[str, list] = {"subtitle": [], "badge": [], "led": []}

    async def fake_subtitle(gateway, text):
        rec["subtitle"].append(text)

    async def fake_badge(gateway, text):
        rec["badge"].append(text)

    async def fake_led(gateway, slot):
        rec["led"].append(slot)

    monkeypatch.setattr(control, "set_device_subtitle", fake_subtitle)
    monkeypatch.setattr(control, "set_device_route_badge", fake_badge)
    # Phase 2: the voice turn drives the LED via apply_led_state(slot)
    # (listening / hermes / idle) rather than the old raw indicator.
    monkeypatch.setattr(control, "apply_led_state", fake_led)
    return rec


def _force_route_hint(monkeypatch, route: str) -> None:
    """Pin the pre-call LED route hint (decide_route) for a turn.

    The bridge lights the "hermes" LED before running the brain when the
    rule-based classifier says Hermes; pin it so the LED sequence is
    deterministic regardless of the local-LLM env.
    """
    from stackchan_mcp import local_llm

    monkeypatch.setattr(local_llm, "is_enabled", lambda: True)
    monkeypatch.setattr(local_llm, "decide_route", lambda _t: route)


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

    async def boom(text, *, force_hermes=False, session_id=None):
        raise RuntimeError("hermes down")

    monkeypatch.setattr(hermes_bridge, "generate_reply", boom)
    gateway = _StubGateway()
    request = _make_voice_request(gateway)

    response = await hermes_bridge.handle_voice_turn(request)

    assert response.status == 502
    assert seen[-1] == control.STATUS_CLEAR
    assert gateway.voice_turn_active is False


# ---- Phase F: subtitle / route badge / LED on the response phase ------


@pytest.mark.asyncio
async def test_voice_turn_hermes_route_sets_badge_and_led(monkeypatch):
    monkeypatch.setenv("STACKCHAN_AUDIO_HOOK_TOKEN", "turn-token")
    _record_status_text(monkeypatch)
    rec = _record_device_cosmetics(monkeypatch)
    _force_route_hint(monkeypatch, "hermes")
    _patch_voice_pipeline(
        monkeypatch, transcript="天気は", reply="晴れだよ", route="hermes"
    )
    gateway = _StubGateway()

    response = await hermes_bridge.handle_voice_turn(_make_voice_request(gateway))

    assert response.status == 200
    # Subtitle: reply shown during TTS, then cleared in finally.
    assert rec["subtitle"] == ["晴れだよ", ""]
    # Badge: "H" set for Hermes, cleared in finally.
    assert rec["badge"] == ["H", ""]
    # LED: listening (STT) → hermes (pre-call thinking + post-call) → idle.
    assert rec["led"] == ["listening", "hermes", "hermes", "idle"]


@pytest.mark.asyncio
async def test_voice_turn_local_route_no_badge_no_led(monkeypatch):
    monkeypatch.setenv("STACKCHAN_AUDIO_HOOK_TOKEN", "turn-token")
    _record_status_text(monkeypatch)
    rec = _record_device_cosmetics(monkeypatch)
    _force_route_hint(monkeypatch, "local")
    _patch_voice_pipeline(
        monkeypatch, transcript="やあ", reply="やあ", route="local"
    )
    gateway = _StubGateway()

    response = await hermes_bridge.handle_voice_turn(_make_voice_request(gateway))

    assert response.status == 200
    # Subtitle still shown + cleared for local turns.
    assert rec["subtitle"] == ["やあ", ""]
    # No badge "H" for local — only the finally clear ("").
    assert rec["badge"] == [""]
    # Local keeps the listening colour (no hermes); finally restores idle.
    assert rec["led"] == ["listening", "idle"]


@pytest.mark.asyncio
async def test_voice_turn_clears_cosmetics_when_tts_fails(monkeypatch):
    import stackchan_mcp.tts.orchestrator as tts_orch

    monkeypatch.setenv("STACKCHAN_AUDIO_HOOK_TOKEN", "turn-token")
    _record_status_text(monkeypatch)
    rec = _record_device_cosmetics(monkeypatch)
    _patch_voice_pipeline(
        monkeypatch, transcript="天気は", reply="晴れ", route="hermes"
    )

    async def boom(arguments, *, gateway=None, **kw):
        raise RuntimeError("tts down")

    monkeypatch.setattr(tts_orch, "synthesize_and_send", boom)
    gateway = _StubGateway()

    response = await hermes_bridge.handle_voice_turn(_make_voice_request(gateway))

    assert response.status == 502
    # Cosmetics were set before TTS, then the finally restores all three
    # (subtitle/badge cleared, LED back to the idle slot).
    assert rec["subtitle"][-1] == ""
    assert rec["badge"][-1] == ""
    assert rec["led"][-1] == "idle"


# ---- conversation log recording hook ---------------------------------


@pytest.mark.asyncio
async def test_voice_turn_records_conversation(monkeypatch):
    monkeypatch.setenv("STACKCHAN_AUDIO_HOOK_TOKEN", "turn-token")
    _record_status_text(monkeypatch)
    control._CONVERSATION.clear()
    _patch_voice_pipeline(
        monkeypatch, transcript="おはよう", reply="やあ", route="local"
    )
    gateway = _StubGateway()

    response = await hermes_bridge.handle_voice_turn(_make_voice_request(gateway))

    assert response.status == 200
    turns = control.get_conversation()["turns"]
    assert len(turns) == 1
    turn = turns[0]
    assert turn["transcript"] == "おはよう"
    assert turn["reply"] == "やあ"
    assert turn["route"] == "local"
    assert turn["timings_ms"] is not None and "total" in turn["timings_ms"]
    control._CONVERSATION.clear()


@pytest.mark.asyncio
async def test_voice_turn_empty_transcript_not_recorded(monkeypatch):
    monkeypatch.setenv("STACKCHAN_AUDIO_HOOK_TOKEN", "turn-token")
    _record_status_text(monkeypatch)
    control._CONVERSATION.clear()
    _patch_voice_pipeline(monkeypatch, transcript="   ")
    gateway = _StubGateway()

    await hermes_bridge.handle_voice_turn(_make_voice_request(gateway))

    # An empty transcript returns before the recording hook.
    assert control.get_conversation()["turns"] == []
    control._CONVERSATION.clear()


@pytest.mark.asyncio
async def test_voice_turn_tts_failure_not_recorded(monkeypatch):
    import stackchan_mcp.tts.orchestrator as tts_orch

    monkeypatch.setenv("STACKCHAN_AUDIO_HOOK_TOKEN", "turn-token")
    _record_status_text(monkeypatch)
    control._CONVERSATION.clear()
    _patch_voice_pipeline(
        monkeypatch, transcript="天気は", reply="晴れ", route="hermes"
    )

    async def boom(arguments, *, gateway=None, **kw):
        raise RuntimeError("tts down")

    monkeypatch.setattr(tts_orch, "synthesize_and_send", boom)
    gateway = _StubGateway()

    response = await hermes_bridge.handle_voice_turn(_make_voice_request(gateway))

    assert response.status == 502
    # A TTS failure returns before the recording hook.
    assert control.get_conversation()["turns"] == []
    control._CONVERSATION.clear()


# ---- Hermes-pin routing toggle (force_hermes) ------------------------


@pytest.mark.asyncio
async def test_generate_reply_force_hermes_bypasses_local(monkeypatch):
    from stackchan_mcp import local_llm

    monkeypatch.setattr(local_llm, "is_enabled", lambda: True)
    monkeypatch.setattr(local_llm, "decide_route", lambda _t: local_llm.ROUTE_LOCAL)
    called = {"local": False}

    async def fake_local(text, *, system_prompt):
        called["local"] = True
        return "ローカル"

    async def fake_hermes(text, *, session_id=None):
        return "ハーメス"

    monkeypatch.setattr(local_llm, "ask_local", fake_local)
    monkeypatch.setattr(hermes_bridge, "ask_hermes", fake_hermes)

    reply, route = await hermes_bridge.generate_reply("短い", force_hermes=True)

    assert (reply, route) == ("ハーメス", local_llm.ROUTE_HERMES)
    assert called["local"] is False  # the local fast-path was skipped


@pytest.mark.asyncio
async def test_generate_reply_default_keeps_local(monkeypatch):
    from stackchan_mcp import local_llm

    monkeypatch.setattr(local_llm, "is_enabled", lambda: True)
    monkeypatch.setattr(local_llm, "decide_route", lambda _t: local_llm.ROUTE_LOCAL)

    async def fake_local(text, *, system_prompt):
        return "ローカル"

    monkeypatch.setattr(local_llm, "ask_local", fake_local)

    reply, route = await hermes_bridge.generate_reply("短い")

    assert (reply, route) == ("ローカル", local_llm.ROUTE_LOCAL)


@pytest.mark.asyncio
async def test_voice_turn_force_hermes_lights_hermes_and_passes_flag(monkeypatch):
    monkeypatch.setenv("STACKCHAN_AUDIO_HOOK_TOKEN", "turn-token")
    _record_status_text(monkeypatch)
    rec = _record_device_cosmetics(monkeypatch)
    # The rule-based classifier would pick LOCAL, but the pin forces Hermes.
    _force_route_hint(monkeypatch, "local")
    _patch_voice_pipeline(
        monkeypatch, transcript="やあ", reply="こんにちは", route="hermes"
    )
    monkeypatch.setattr(control, "routing_force_hermes", lambda: True)
    seen: dict[str, bool] = {}

    async def fake_gr(text, *, force_hermes=False, session_id=None):
        seen["force_hermes"] = force_hermes
        return "こんにちは", "hermes"

    monkeypatch.setattr(hermes_bridge, "generate_reply", fake_gr)
    gateway = _StubGateway()

    response = await hermes_bridge.handle_voice_turn(_make_voice_request(gateway))

    assert response.status == 200
    # The pin is threaded into generate_reply.
    assert seen["force_hermes"] is True
    # LED hint lights Hermes pre-call despite decide_route == local.
    assert rec["led"] == ["listening", "hermes", "hermes", "idle"]


# ---- multi-turn continuation (Phase 1) -----------------------------------


def _enable_multiturn(monkeypatch, *, muted: bool = False) -> None:
    """Turn the feature on with no guard sleep, unmuted by default."""
    monkeypatch.setenv("STACKCHAN_MULTITURN", "1")
    monkeypatch.setenv("MULTITURN_TTS_GUARD_MS", "0")
    monkeypatch.setattr(control, "is_muted", lambda: muted)


def _run_one_turn(monkeypatch, gateway, *, transcript="やあ", reply="はい", route="hermes"):
    monkeypatch.setenv("STACKCHAN_AUDIO_HOOK_TOKEN", "turn-token")
    _record_status_text(monkeypatch)
    _record_device_cosmetics(monkeypatch)
    _patch_voice_pipeline(monkeypatch, transcript=transcript, reply=reply, route=route)


@pytest.mark.asyncio
async def test_multiturn_reopens_listen_on_hermes_question(monkeypatch):
    _run_one_turn(monkeypatch, None, reply="元気にしてた？", route="hermes")
    _enable_multiturn(monkeypatch)
    gateway = _StubGateway()

    response = await hermes_bridge.handle_voice_turn(_make_voice_request(gateway))

    assert response.status == 200
    # A continuation listen was fired, the counter advanced, and the gap
    # flag stays True so the heartbeat is suppressed until the answer.
    assert gateway.esp32.listen_calls == [("start", "manual")]
    assert gateway.multiturn.turn_count == 1
    assert gateway.multiturn_active is True
    body = json.loads(response.body)
    assert body["multiturn"] is True


@pytest.mark.asyncio
async def test_multiturn_off_by_default(monkeypatch):
    # No STACKCHAN_MULTITURN env: feature disabled even on a question.
    monkeypatch.delenv("STACKCHAN_MULTITURN", raising=False)
    _run_one_turn(monkeypatch, None, reply="元気？", route="hermes")
    gateway = _StubGateway()

    response = await hermes_bridge.handle_voice_turn(_make_voice_request(gateway))

    assert response.status == 200
    assert gateway.esp32.listen_calls == []
    assert gateway.multiturn_active is False
    assert json.loads(response.body)["multiturn"] is False


@pytest.mark.asyncio
async def test_multiturn_skips_local_route(monkeypatch):
    _run_one_turn(monkeypatch, None, reply="元気？", route="local")
    _enable_multiturn(monkeypatch)
    gateway = _StubGateway()

    await hermes_bridge.handle_voice_turn(_make_voice_request(gateway))

    assert gateway.esp32.listen_calls == []
    assert gateway.multiturn_active is False


@pytest.mark.asyncio
async def test_multiturn_skips_non_question(monkeypatch):
    _run_one_turn(monkeypatch, None, reply="そうなんだ。", route="hermes")
    _enable_multiturn(monkeypatch)
    gateway = _StubGateway()

    await hermes_bridge.handle_voice_turn(_make_voice_request(gateway))

    assert gateway.esp32.listen_calls == []


@pytest.mark.asyncio
async def test_multiturn_stops_at_ceiling(monkeypatch):
    _run_one_turn(monkeypatch, None, reply="まだ続ける？", route="hermes")
    _enable_multiturn(monkeypatch)
    monkeypatch.setenv("MAX_MULTITURN_TURNS", "2")
    gateway = _StubGateway()
    # Mid-conversation at the ceiling: a *fresh* gap (recent activity) so
    # the entry stale-reset does not fire and the ceiling check applies.
    monkeypatch.setattr(hermes_bridge.time, "monotonic", lambda: 500.0)
    gateway.multiturn.turn_count = 2  # already at the ceiling
    gateway.multiturn.last_activity = 500.0

    await hermes_bridge.handle_voice_turn(_make_voice_request(gateway))

    assert gateway.esp32.listen_calls == []
    # Reaching the ceiling ends the conversation: the counter resets.
    assert gateway.multiturn.turn_count == 0


@pytest.mark.asyncio
async def test_multiturn_ceiling_on_question_shows_tap_hint(monkeypatch):
    # Phase 3 UX: when we stop only because the turn ceiling was hit while
    # Hermes still had an open question, leave a "tap to continue" subtitle
    # instead of blanking the display.
    monkeypatch.setenv("STACKCHAN_AUDIO_HOOK_TOKEN", "turn-token")
    _record_status_text(monkeypatch)
    rec = _record_device_cosmetics(monkeypatch)
    _patch_voice_pipeline(
        monkeypatch, transcript="やあ", reply="まだ続ける？", route="hermes"
    )
    _enable_multiturn(monkeypatch)
    monkeypatch.setenv("MAX_MULTITURN_TURNS", "2")
    monkeypatch.setattr(hermes_bridge.time, "monotonic", lambda: 500.0)
    gateway = _StubGateway()
    gateway.multiturn.turn_count = 2  # already at the ceiling
    gateway.multiturn.last_activity = 500.0

    await hermes_bridge.handle_voice_turn(_make_voice_request(gateway))

    assert gateway.esp32.listen_calls == []  # no hands-free re-listen
    assert gateway.multiturn_active is False
    # The last subtitle written is the hint (after the reply subtitle), and
    # the one-shot flag is consumed.
    assert rec["subtitle"][-1] == multiturn.TAP_TO_CONTINUE_HINT
    assert gateway.multiturn_prompt_pending is False


@pytest.mark.asyncio
async def test_multiturn_no_hint_on_normal_end(monkeypatch):
    # A non-question end clears the subtitle as before — the hint is only
    # for the ceiling-on-question case, not every conversation close.
    monkeypatch.setenv("STACKCHAN_AUDIO_HOOK_TOKEN", "turn-token")
    _record_status_text(monkeypatch)
    rec = _record_device_cosmetics(monkeypatch)
    _patch_voice_pipeline(
        monkeypatch, transcript="やあ", reply="そうだね。", route="hermes"
    )
    _enable_multiturn(monkeypatch)
    gateway = _StubGateway()

    await hermes_bridge.handle_voice_turn(_make_voice_request(gateway))

    assert gateway.multiturn_prompt_pending is False
    assert rec["subtitle"][-1] == ""


@pytest.mark.asyncio
async def test_multiturn_skips_when_muted(monkeypatch):
    _run_one_turn(monkeypatch, None, reply="元気？", route="hermes")
    _enable_multiturn(monkeypatch, muted=True)
    gateway = _StubGateway()

    await hermes_bridge.handle_voice_turn(_make_voice_request(gateway))

    assert gateway.esp32.listen_calls == []


@pytest.mark.asyncio
async def test_multiturn_skips_when_disconnected(monkeypatch):
    _run_one_turn(monkeypatch, None, reply="元気？", route="hermes")
    _enable_multiturn(monkeypatch)
    gateway = _StubGateway()
    gateway.esp32.device_connected = False

    await hermes_bridge.handle_voice_turn(_make_voice_request(gateway))

    assert gateway.esp32.listen_calls == []


@pytest.mark.asyncio
async def test_multiturn_empty_transcript_resets_counter(monkeypatch):
    _run_one_turn(monkeypatch, None, transcript="   ", reply="ignored", route="hermes")
    _enable_multiturn(monkeypatch)
    gateway = _StubGateway()
    gateway.multiturn.turn_count = 2  # mid-conversation

    response = await hermes_bridge.handle_voice_turn(_make_voice_request(gateway))

    # Silence ends the conversation: no re-listen, counter cleared.
    assert response.status == 200
    assert json.loads(response.body)["ok"] is False
    assert gateway.esp32.listen_calls == []
    assert gateway.multiturn.turn_count == 0


@pytest.mark.asyncio
async def test_voice_turn_threads_rotating_conversation_id(monkeypatch):
    """Phase 2: the voice turn threads a per-conversation Hermes id into
    the brain call — reused within the context window, rotated past it."""
    monkeypatch.setenv("STACKCHAN_AUDIO_HOOK_TOKEN", "turn-token")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)  # default base
    monkeypatch.delenv("HERMES_SESSION_WINDOW_S", raising=False)  # default 180
    _record_status_text(monkeypatch)
    _record_device_cosmetics(monkeypatch)
    _patch_voice_pipeline(monkeypatch, transcript="やあ", reply="はい", route="hermes")

    seen: list[str | None] = []

    async def capture_gr(text, *, force_hermes=False, session_id=None):
        seen.append(session_id)
        return "はい", "hermes"

    monkeypatch.setattr(hermes_bridge, "generate_reply", capture_gr)
    gateway = _StubGateway()

    # Two turns 10 s apart share one conversation id (< 180 s window)...
    monkeypatch.setattr(hermes_bridge.time, "monotonic", lambda: 1000.0)
    await hermes_bridge.handle_voice_turn(_make_voice_request(gateway))
    monkeypatch.setattr(hermes_bridge.time, "monotonic", lambda: 1010.0)
    await hermes_bridge.handle_voice_turn(_make_voice_request(gateway))
    # ...then a long gap starts a fresh conversation (new id).
    monkeypatch.setattr(hermes_bridge.time, "monotonic", lambda: 5000.0)
    await hermes_bridge.handle_voice_turn(_make_voice_request(gateway))

    assert all(s and s.startswith("stackchan-voice-") for s in seen)
    assert seen[0] == seen[1]  # same conversation, context retained
    assert seen[2] != seen[0]  # rotated after the window


@pytest.mark.asyncio
async def test_voice_turn_window_zero_uses_fixed_session_id(monkeypatch):
    """HERMES_SESSION_WINDOW_S=0 disables rotation — every turn carries
    the fixed HERMES_SESSION_ID, exactly as before Phase 2."""
    monkeypatch.setenv("STACKCHAN_AUDIO_HOOK_TOKEN", "turn-token")
    monkeypatch.setenv("HERMES_SESSION_ID", "stackchan-voice")
    monkeypatch.setenv("HERMES_SESSION_WINDOW_S", "0")
    _record_status_text(monkeypatch)
    _record_device_cosmetics(monkeypatch)
    _patch_voice_pipeline(monkeypatch, transcript="やあ", reply="はい", route="hermes")

    seen: list[str | None] = []

    async def capture_gr(text, *, force_hermes=False, session_id=None):
        seen.append(session_id)
        return "はい", "hermes"

    monkeypatch.setattr(hermes_bridge, "generate_reply", capture_gr)
    gateway = _StubGateway()

    monkeypatch.setattr(hermes_bridge.time, "monotonic", lambda: 1000.0)
    await hermes_bridge.handle_voice_turn(_make_voice_request(gateway))
    monkeypatch.setattr(hermes_bridge.time, "monotonic", lambda: 9000.0)
    await hermes_bridge.handle_voice_turn(_make_voice_request(gateway))

    # No rotation: the base id is used verbatim on every turn.
    assert seen == ["stackchan-voice", "stackchan-voice"]


@pytest.mark.asyncio
async def test_multiturn_stale_gap_resets_at_turn_entry(monkeypatch):
    _run_one_turn(monkeypatch, None, reply="そうだね。", route="hermes")
    _enable_multiturn(monkeypatch)
    monkeypatch.setenv("MULTITURN_SESSION_TIMEOUT_S", "60")
    gateway = _StubGateway()
    # An old, abandoned gap: counter set, activity far in the past.
    gateway.multiturn.turn_count = 3
    gateway.multiturn.last_activity = 1.0
    monkeypatch.setattr(hermes_bridge.time, "monotonic", lambda: 1000.0)

    await hermes_bridge.handle_voice_turn(_make_voice_request(gateway))

    # The stale gap was reset at entry; this fresh turn (no question)
    # leaves the counter at 0.
    assert gateway.multiturn.turn_count == 0
    assert gateway.multiturn_active is False


@pytest.mark.asyncio
async def test_multiturn_continuation_skips_display_clear(monkeypatch):
    # When a turn re-opens listening, the finally must NOT clear the
    # status text (on_listen_started owns the listening display now).
    monkeypatch.setenv("STACKCHAN_AUDIO_HOOK_TOKEN", "turn-token")
    seen = _record_status_text(monkeypatch)
    _record_device_cosmetics(monkeypatch)
    _patch_voice_pipeline(monkeypatch, transcript="やあ", reply="元気？", route="hermes")
    _enable_multiturn(monkeypatch)
    gateway = _StubGateway()

    await hermes_bridge.handle_voice_turn(_make_voice_request(gateway))

    # きいてるよ → 考え中, but NO trailing clear (would blank the re-listen).
    assert control.STATUS_CLEAR not in seen
    assert seen == [control.STATUS_LISTENING, control.STATUS_THINKING]
