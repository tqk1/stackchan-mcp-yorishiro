"""Tests for the Hermes voice bridge (ask_hermes request shape)."""

from typing import Any

import pytest
from aiohttp import web

from stackchan_mcp import hermes_bridge
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
