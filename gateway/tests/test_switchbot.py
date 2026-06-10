"""Tests for the SwitchBot cloud client and its MCP tool surface."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

import pytest
from aiohttp import web
from mcp.types import CallToolRequest, ListToolsRequest

import stackchan_mcp.stdio_server as stdio_server
from stackchan_mcp import switchbot
from stackchan_mcp.stdio_server import create_server
from stackchan_mcp.switchbot import (
    TOOL_NAMES,
    build_auth_headers,
    get_device_status,
    is_configured,
    list_devices,
    send_command,
)


# --- signature (v1.1 auth headers) --------------------------------------------


def test_build_auth_headers_known_vector():
    """sign = base64(HMAC-SHA256(secret, token + t + nonce)) — fixed vector."""
    headers = build_auth_headers(
        "test-token",
        "test-secret",
        t=1700000000000,
        nonce="8c52f24e-0000-4000-8000-000000000000",
    )
    assert headers["Authorization"] == "test-token"
    assert headers["t"] == "1700000000000"
    assert headers["nonce"] == "8c52f24e-0000-4000-8000-000000000000"
    assert headers["sign"] == "hWRO8tYQIz+I7hgALpe2O5u3z0X6vdeUG4OH5DsLwL0="


def test_build_auth_headers_generates_t_and_nonce():
    """Without injected t/nonce, a fresh 13-digit t and UUID nonce appear
    and the sign is consistent with them."""
    headers = build_auth_headers("tok", "sec")
    assert len(headers["t"]) == 13 and headers["t"].isdigit()
    assert len(headers["nonce"]) == 36
    expected = base64.b64encode(
        hmac.new(
            b"sec",
            msg=f"tok{headers['t']}{headers['nonce']}".encode(),
            digestmod=hashlib.sha256,
        ).digest()
    ).decode()
    assert headers["sign"] == expected


# --- configuration gate ---------------------------------------------------------


def test_is_configured_requires_both_envs(monkeypatch):
    monkeypatch.delenv("SWITCHBOT_TOKEN", raising=False)
    monkeypatch.delenv("SWITCHBOT_SECRET", raising=False)
    assert is_configured() is False
    monkeypatch.setenv("SWITCHBOT_TOKEN", "tok")
    assert is_configured() is False
    monkeypatch.setenv("SWITCHBOT_SECRET", "   ")
    assert is_configured() is False
    monkeypatch.setenv("SWITCHBOT_SECRET", "sec")
    assert is_configured() is True


@pytest.mark.asyncio
async def test_unconfigured_call_raises_clear_error(monkeypatch):
    monkeypatch.delenv("SWITCHBOT_TOKEN", raising=False)
    monkeypatch.delenv("SWITCHBOT_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="SWITCHBOT_TOKEN"):
        await list_devices()


# --- HTTP client (stubbed SwitchBot API) ----------------------------------------


async def _run_switchbot_stub(
    routes: list[tuple[str, str, Any]], aiohttp_unused_port
) -> tuple[web.AppRunner, str]:
    app = web.Application()
    for method, path, handler in routes:
        app.router.add_route(method, path, handler)
    port = aiohttp_unused_port()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner, f"http://127.0.0.1:{port}"


def _configure(monkeypatch, base_url: str) -> None:
    monkeypatch.setenv("SWITCHBOT_TOKEN", "test-token")
    monkeypatch.setenv("SWITCHBOT_SECRET", "test-secret")
    monkeypatch.setenv("SWITCHBOT_API_URL", base_url)


@pytest.mark.asyncio
async def test_list_devices_success(monkeypatch, aiohttp_unused_port):
    """GET /devices carries valid auth headers and returns the body."""
    received: dict[str, Any] = {}
    device_body = {
        "deviceList": [
            {
                "deviceId": "AABBCCDDEEFF",
                "deviceName": "リビングのプラグ",
                "deviceType": "Plug Mini (JP)",
            }
        ],
        "infraredRemoteList": [
            {
                "deviceId": "02-202401010000-12345678",
                "deviceName": "リビングの電気",
                "remoteType": "Light",
            }
        ],
    }

    async def handle(request: web.Request) -> web.Response:
        received["headers"] = dict(request.headers)
        return web.json_response(
            {"statusCode": 100, "message": "success", "body": device_body}
        )

    runner, base_url = await _run_switchbot_stub(
        [("GET", "/devices", handle)], aiohttp_unused_port
    )
    _configure(monkeypatch, base_url)
    try:
        result = await list_devices()
    finally:
        await runner.cleanup()

    assert result == device_body
    headers = received["headers"]
    assert headers["Authorization"] == "test-token"
    # sign must be the HMAC of token + t + nonce as actually sent.
    expected_sign = base64.b64encode(
        hmac.new(
            b"test-secret",
            msg=f"test-token{headers['t']}{headers['nonce']}".encode(),
            digestmod=hashlib.sha256,
        ).digest()
    ).decode()
    assert headers["sign"] == expected_sign


@pytest.mark.asyncio
async def test_get_device_status_success(monkeypatch, aiohttp_unused_port):
    status_body = {"deviceId": "AABBCCDDEEFF", "power": "on", "voltage": 100}

    async def handle(request: web.Request) -> web.Response:
        assert request.match_info["device_id"] == "AABBCCDDEEFF"
        return web.json_response(
            {"statusCode": 100, "message": "success", "body": status_body}
        )

    runner, base_url = await _run_switchbot_stub(
        [("GET", "/devices/{device_id}/status", handle)], aiohttp_unused_port
    )
    _configure(monkeypatch, base_url)
    try:
        result = await get_device_status("AABBCCDDEEFF")
    finally:
        await runner.cleanup()

    assert result == status_body


@pytest.mark.asyncio
async def test_send_command_posts_expected_payload(monkeypatch, aiohttp_unused_port):
    """Defaults: parameter='default', commandType='command' (IR turnOn case)."""
    received: dict[str, Any] = {}

    async def handle(request: web.Request) -> web.Response:
        received["device_id"] = request.match_info["device_id"]
        received["payload"] = await request.json()
        return web.json_response(
            {"statusCode": 100, "message": "success", "body": {}}
        )

    runner, base_url = await _run_switchbot_stub(
        [("POST", "/devices/{device_id}/commands", handle)], aiohttp_unused_port
    )
    _configure(monkeypatch, base_url)
    try:
        result = await send_command("02-202401010000-12345678", "turnOn")
    finally:
        await runner.cleanup()

    assert result == {}
    assert received["device_id"] == "02-202401010000-12345678"
    assert received["payload"] == {
        "command": "turnOn",
        "parameter": "default",
        "commandType": "command",
    }


@pytest.mark.asyncio
async def test_http_error_status_raises(monkeypatch, aiohttp_unused_port):
    async def handle(request: web.Request) -> web.Response:
        return web.json_response({"message": "Unauthorized"}, status=401)

    runner, base_url = await _run_switchbot_stub(
        [("GET", "/devices", handle)], aiohttp_unused_port
    )
    _configure(monkeypatch, base_url)
    try:
        with pytest.raises(RuntimeError, match="status=401"):
            await list_devices()
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_body_status_code_error_raises(monkeypatch, aiohttp_unused_port):
    """HTTP 200 with statusCode != 100 is still a failure (e.g. 190)."""

    async def handle(request: web.Request) -> web.Response:
        return web.json_response(
            {"statusCode": 190, "message": "device internal error", "body": {}}
        )

    runner, base_url = await _run_switchbot_stub(
        [("GET", "/devices", handle)], aiohttp_unused_port
    )
    _configure(monkeypatch, base_url)
    try:
        with pytest.raises(RuntimeError, match="statusCode=190"):
            await list_devices()
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_connection_failure_raises_runtime_error(
    monkeypatch, aiohttp_unused_port
):
    """Transport errors surface as RuntimeError, not raw aiohttp exceptions."""
    _configure(monkeypatch, f"http://127.0.0.1:{aiohttp_unused_port()}")
    with pytest.raises(RuntimeError, match="request failed"):
        await list_devices()


@pytest.mark.asyncio
async def test_missing_device_id_raises_value_error(monkeypatch):
    monkeypatch.setenv("SWITCHBOT_TOKEN", "tok")
    monkeypatch.setenv("SWITCHBOT_SECRET", "sec")
    with pytest.raises(ValueError, match="device_id"):
        await get_device_status("  ")
    with pytest.raises(ValueError, match="command"):
        await send_command("AABBCCDDEEFF", "")


# --- MCP tool surface (stdio + shared HTTP dispatch) -----------------------------


class _FakeESP32:
    device_connected = False  # SwitchBot tools must not require the device


class _FakeGateway:
    esp32 = _FakeESP32()


@pytest.mark.asyncio
async def test_list_tools_includes_switchbot_tools():
    """All three SwitchBot tools are always listed (same convention as
    say/listen: visible even when unconfigured, clear error on call)."""
    server = create_server()
    result = await server.request_handlers[ListToolsRequest](
        ListToolsRequest(method="tools/list")
    )
    tool_names = {tool.name for tool in result.root.tools}
    assert TOOL_NAMES <= tool_names

    send_tool = next(
        t for t in result.root.tools if t.name == "switchbot_send_command"
    )
    assert set(send_tool.inputSchema["required"]) == {"device_id", "command"}


@pytest.mark.asyncio
async def test_dispatch_send_command_via_mcp(monkeypatch):
    """The MCP layer maps tool arguments onto switchbot.send_command,
    without touching the (disconnected) ESP32."""
    calls: list[tuple[Any, ...]] = []

    async def fake_send_command(device_id, command, parameter, command_type):
        calls.append((device_id, command, parameter, command_type))
        return {}

    monkeypatch.setattr(switchbot, "send_command", fake_send_command)
    monkeypatch.setattr(stdio_server, "get_gateway", lambda: _FakeGateway())
    server = create_server()

    result = await server.request_handlers[CallToolRequest](
        CallToolRequest(
            method="tools/call",
            params={
                "name": "switchbot_send_command",
                "arguments": {"device_id": "AABBCCDDEEFF", "command": "turnOn"},
            },
        )
    )

    assert calls == [("AABBCCDDEEFF", "turnOn", "default", "command")]
    assert json.loads(result.root.content[0].text) == {"ok": True, "result": {}}


@pytest.mark.asyncio
async def test_dispatch_unconfigured_returns_error_payload(monkeypatch):
    """Unset credentials: the tool call returns a clear error payload
    instead of raising, so listing/serving is never broken."""
    monkeypatch.delenv("SWITCHBOT_TOKEN", raising=False)
    monkeypatch.delenv("SWITCHBOT_SECRET", raising=False)
    monkeypatch.setattr(stdio_server, "get_gateway", lambda: _FakeGateway())
    server = create_server()

    result = await server.request_handlers[CallToolRequest](
        CallToolRequest(
            method="tools/call",
            params={"name": "switchbot_list_devices", "arguments": {}},
        )
    )

    payload = json.loads(result.root.content[0].text)
    assert "SWITCHBOT_TOKEN" in payload["error"]


@pytest.mark.asyncio
async def test_dispatch_blank_device_id_returns_error_payload(monkeypatch):
    """A whitespace-only device_id passes the JSON schema but is rejected
    by the dispatch layer with an error payload (no exception). A fully
    missing device_id is already rejected by the SDK's schema validation."""
    monkeypatch.setenv("SWITCHBOT_TOKEN", "tok")
    monkeypatch.setenv("SWITCHBOT_SECRET", "sec")
    monkeypatch.setattr(stdio_server, "get_gateway", lambda: _FakeGateway())
    server = create_server()

    result = await server.request_handlers[CallToolRequest](
        CallToolRequest(
            method="tools/call",
            params={"name": "switchbot_get_status", "arguments": {"device_id": "  "}},
        )
    )

    payload = json.loads(result.root.content[0].text)
    assert "device_id" in payload["error"]


# --- helpers ---------------------------------------------------------------------


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
