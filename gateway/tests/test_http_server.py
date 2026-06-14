from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from mcp.server.streamable_http import MCP_SESSION_ID_HEADER
from mcp.types import ErrorData, TextContent

from stackchan_mcp.http_server import (
    AUTH_FAILURE_MESSAGE,
    BYPASS_TOOLS,
    DISCONNECTED_DEVICE_PAYLOAD,
    HOST_FAILURE_MESSAGE,
    ORIGIN_FAILURE_MESSAGE,
    MCP_HTTP_ALLOWED_HOSTS_ENV,
    build_app,
    make_dispatch_fn,
)
from stackchan_mcp.queue import CommandQueue, QueueFull, QueueItem, build_queue_full_error


class FakeESP32:
    def __init__(self, *, connected: bool = True) -> None:
        self.device_connected = connected
        self.calls: list[tuple[str, dict]] = []

    def get_status(self) -> dict:
        return {
            "connected": self.device_connected,
            "device": "fake-stackchan",
        }

    async def call_tool(self, name: str, arguments: dict) -> tuple[dict, None]:
        self.calls.append((name, arguments))
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({"name": name, "arguments": arguments}),
                }
            ],
        }, None


class FakeGateway:
    def __init__(self, *, connected: bool = True) -> None:
        self.esp32 = FakeESP32(connected=connected)


@contextlib.asynccontextmanager
async def _client(app, *, base_url: str = "http://127.0.0.1:8767") -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
            yield client


def _headers(session_id: str | None = None, token: str | None = None) -> dict[str, str]:
    headers = {"accept": "application/json"}
    if session_id is not None:
        headers[MCP_SESSION_ID_HEADER] = session_id
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    return headers


async def _initialize(client: httpx.AsyncClient, *, token: str | None = None, request_id: int = 1) -> str:
    response = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        },
        headers=_headers(token=token),
    )
    assert response.status_code == 200
    session_id = response.headers[MCP_SESSION_ID_HEADER]
    initialized = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=_headers(session_id, token),
    )
    assert initialized.status_code == 202
    return session_id


async def _call_tool(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    name: str,
    arguments: dict | None = None,
    request_id: int | str = 2,
    token: str | None = None,
) -> httpx.Response:
    return await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
        headers=_headers(session_id, token),
    )


async def _wait_for_queue_depth(queue: CommandQueue, depth: int) -> None:
    for _ in range(50):
        if queue.depth == depth:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"queue depth did not reach {depth}")


class GatedCommandQueue(CommandQueue):
    def __init__(self, capacity: int) -> None:
        super().__init__(capacity=capacity)
        self.allow_get = asyncio.Event()
        self.get_started = asyncio.Event()
        self.enqueued_task: asyncio.Task | None = None
        self.last_enqueued_item: QueueItem | None = None

    def enqueue(self, item: QueueItem) -> None:
        task = asyncio.current_task()
        self.enqueued_task = task if isinstance(task, asyncio.Task) else None
        self.last_enqueued_item = item
        super().enqueue(item)

    async def get(self) -> QueueItem:
        self.get_started.set()
        await self.allow_get.wait()
        return await super().get()


@pytest.mark.asyncio
async def test_queue_ordering_fifo_completion() -> None:
    queue = CommandQueue(capacity=3)
    observed: list[str] = []
    loop = asyncio.get_running_loop()
    futures = [loop.create_future() for _ in range(3)]

    for index, future in enumerate(futures):
        queue.enqueue(
            QueueItem(
                correlation_id=f"item-{index}",
                client_session_id=None,
                client_request_id=index,
                tool_name=f"tool-{index}",
                arguments={},
                response_future=future,
                enqueued_at=0.0,
            )
        )

    async def dispatch(item: QueueItem) -> str:
        observed.append(item.tool_name)
        return item.tool_name

    dispatcher = asyncio.create_task(queue.run_dispatcher(dispatch))
    try:
        results = await asyncio.gather(*futures)
    finally:
        dispatcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await dispatcher

    assert observed == ["tool-0", "tool-1", "tool-2"]
    assert results == observed


@pytest.mark.asyncio
async def test_queue_full_returns_jsonrpc_error_response() -> None:
    queue = CommandQueue(capacity=1)
    app = build_app(
        queue,
        gateway=FakeGateway(),
        owner_id="owner-test",
        host="127.0.0.1",
        port=8767,
    )

    async with _client(app) as client:
        session_id = await _initialize(client)
        first = asyncio.create_task(
            _call_tool(client, session_id=session_id, name="get_device_info", request_id=10)
        )
        await _wait_for_queue_depth(queue, 1)
        second = await _call_tool(
            client,
            session_id=session_id,
            name="get_head_angles",
            request_id=11,
        )
        first.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first

    assert second.status_code == 200
    payload = second.json()
    assert payload["id"] == 11
    assert payload["error"] == build_queue_full_error(1)


@pytest.mark.asyncio
async def test_cancelled_client_item_is_not_dispatched() -> None:
    queue = GatedCommandQueue(capacity=2)
    dispatched: list[str] = []

    async def dispatch(item: QueueItem):
        dispatched.append(item.tool_name)
        return [TextContent(type="text", text=json.dumps({"ok": True}))]

    app = build_app(
        queue,
        gateway=FakeGateway(),
        owner_id="owner-test",
        host="127.0.0.1",
        port=8767,
        dispatch_fn=dispatch,
    )

    async with _client(app) as client:
        session_id = await _initialize(client)
        call_task = asyncio.create_task(
            _call_tool(
                client,
                session_id=session_id,
                name="get_device_info",
            )
        )
        await _wait_for_queue_depth(queue, 1)

        assert queue.enqueued_task is not None
        queue.enqueued_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await call_task
        assert queue.last_enqueued_item is not None
        assert queue.last_enqueued_item.response_future.cancelled()

        queue.allow_get.set()
        await _wait_for_queue_depth(queue, 0)
        await asyncio.sleep(0.01)

    assert dispatched == []


@pytest.mark.asyncio
async def test_lifespan_shutdown_drains_pending_queue_items() -> None:
    queue = GatedCommandQueue(capacity=3)
    dispatched: list[str] = []

    async def dispatch(item: QueueItem):
        dispatched.append(item.tool_name)
        return [TextContent(type="text", text=json.dumps({"ok": True}))]

    app = build_app(
        queue,
        gateway=FakeGateway(),
        owner_id="owner-test",
        host="127.0.0.1",
        port=8767,
        dispatch_fn=dispatch,
    )

    loop = asyncio.get_running_loop()
    futures = [loop.create_future() for _ in range(2)]

    async with app.router.lifespan_context(app):
        for index, future in enumerate(futures):
            queue.enqueue(
                QueueItem(
                    correlation_id=f"pending-{index}",
                    client_session_id=None,
                    client_request_id=index,
                    tool_name=f"tool-{index}",
                    arguments={},
                    response_future=future,
                    enqueued_at=0.0,
                )
            )
        await _wait_for_queue_depth(queue, 2)

    assert queue.depth == 0
    assert dispatched == []
    for future in futures:
        assert future.done()
        result = future.result()
        assert isinstance(result, ErrorData)
        assert result.code == -32000
        assert result.message == "stackchan MCP HTTP server is shutting down"
        assert result.data == {"reason": "server_shutdown"}


@pytest.mark.asyncio
async def test_auth_rejection_and_successful_bearer_reaches_dispatcher() -> None:
    queue = CommandQueue(capacity=4)
    dispatched: list[str] = []

    async def dispatch(item: QueueItem):
        dispatched.append(item.tool_name)
        return [TextContent(type="text", text=json.dumps({"ok": True}))]

    app = build_app(
        queue,
        gateway=FakeGateway(),
        owner_id="owner-test",
        host="127.0.0.1",
        port=8767,
        token="secret",
        dispatch_fn=dispatch,
    )

    async with _client(app) as client:
        missing = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers=_headers(),
        )
        wrong = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers=_headers(token="wrong"),
        )
        session_id = await _initialize(client, token="secret")
        ok = await _call_tool(
            client,
            session_id=session_id,
            token="secret",
            name="get_device_info",
        )

    assert missing.status_code == 401
    assert missing.text == AUTH_FAILURE_MESSAGE
    assert wrong.status_code == 401
    assert ok.status_code == 200
    assert dispatched == ["get_device_info"]


@pytest.mark.asyncio
async def test_host_and_origin_rebinding_guards_return_403() -> None:
    app = build_app(
        CommandQueue(capacity=2),
        gateway=FakeGateway(),
        owner_id="owner-test",
        host="127.0.0.1",
        port=8767,
    )

    async with _client(app) as client:
        bad_host = await client.get(
            "/healthz",
            headers={"host": "evil.example:8767"},
        )
        bad_origin = await client.get(
            "/healthz",
            headers={"origin": "http://evil.example:8767"},
        )

    assert bad_host.status_code == 403
    assert bad_host.text == HOST_FAILURE_MESSAGE
    assert bad_origin.status_code == 403
    assert bad_origin.text == ORIGIN_FAILURE_MESSAGE


@pytest.mark.asyncio
async def test_wildcard_bind_allows_loopback_and_configured_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        MCP_HTTP_ALLOWED_HOSTS_ENV,
        "192.168.1.10, https://stackchan.example.test:9443",
    )
    app = build_app(
        CommandQueue(capacity=2),
        gateway=FakeGateway(),
        owner_id="owner-test",
        host="0.0.0.0",
        port=8767,
    )

    async with _client(app) as client:
        loopback = await client.get("/healthz", headers={"host": "127.0.0.1:8767"})
        lan = await client.get("/healthz", headers={"host": "192.168.1.10:8767"})
        origin = await client.get(
            "/healthz",
            headers={
                "host": "stackchan.example.test:9443",
                "origin": "https://stackchan.example.test:9443",
            },
        )

    assert loopback.status_code == 200
    assert lan.status_code == 200
    assert origin.status_code == 200


@pytest.mark.asyncio
async def test_response_correlation_for_two_concurrent_clients() -> None:
    queue = CommandQueue(capacity=4)

    async def dispatch(item: QueueItem):
        await asyncio.sleep(0.01)
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "client_request_id": item.client_request_id,
                        "tool_name": item.tool_name,
                    }
                ),
            )
        ]

    app = build_app(
        queue,
        gateway=FakeGateway(),
        owner_id="owner-test",
        host="127.0.0.1",
        port=8767,
        dispatch_fn=dispatch,
    )

    async with _client(app) as client:
        session_a = await _initialize(client, request_id=1)
        session_b = await _initialize(client, request_id=2)
        response_a, response_b = await asyncio.gather(
            _call_tool(
                client,
                session_id=session_a,
                name="get_device_info",
                request_id="client-a",
            ),
            _call_tool(
                client,
                session_id=session_b,
                name="get_head_angles",
                request_id="client-b",
            ),
        )

    payload_a = response_a.json()
    payload_b = response_b.json()
    assert payload_a["id"] == "client-a"
    assert payload_b["id"] == "client-b"
    body_a = json.loads(payload_a["result"]["content"][0]["text"])
    body_b = json.loads(payload_b["result"]["content"][0]["text"])
    assert body_a["client_request_id"] == "client-a"
    assert body_b["client_request_id"] == "client-b"


@pytest.mark.asyncio
async def test_bypass_tool_get_status_does_not_enter_dispatcher() -> None:
    assert BYPASS_TOOLS == frozenset(
        {
            "get_status",
            "switchbot_list_devices",
            "switchbot_get_status",
            "switchbot_send_command",
            "web_search",
            "write_note",
            "read_note",
            "list_notes",
        }
    )
    queue = CommandQueue(capacity=2)

    async def dispatch(_item: QueueItem):
        raise AssertionError("get_status must bypass the command queue")

    app = build_app(
        queue,
        gateway=FakeGateway(),
        owner_id="owner-test",
        host="127.0.0.1",
        port=8767,
        dispatch_fn=dispatch,
    )

    async with _client(app) as client:
        session_id = await _initialize(client)
        response = await _call_tool(client, session_id=session_id, name="get_status")

    payload = response.json()
    status = json.loads(payload["result"]["content"][0]["text"])
    assert status["connected"] is True
    assert queue.depth == 0


@pytest.mark.asyncio
async def test_switchbot_tools_exposed_and_bypass_device_queue(monkeypatch) -> None:
    """SwitchBot tools are listed over Streamable HTTP and dispatch
    gateway-locally: no queue entry, no ESP32 — even when the device is
    disconnected the call reaches the SwitchBot layer (here: the
    unconfigured-credentials error, not the disconnected-device payload)."""
    monkeypatch.delenv("SWITCHBOT_TOKEN", raising=False)
    monkeypatch.delenv("SWITCHBOT_SECRET", raising=False)
    queue = CommandQueue(capacity=2)

    async def dispatch(_item: QueueItem):
        raise AssertionError("switchbot tools must bypass the command queue")

    app = build_app(
        queue,
        gateway=FakeGateway(connected=False),
        owner_id="owner-test",
        host="127.0.0.1",
        port=8767,
        dispatch_fn=dispatch,
    )

    async with _client(app) as client:
        session_id = await _initialize(client)
        listing = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 5, "method": "tools/list"},
            headers=_headers(session_id),
        )
        response = await _call_tool(
            client, session_id=session_id, name="switchbot_list_devices"
        )

    tool_names = {tool["name"] for tool in listing.json()["result"]["tools"]}
    assert {
        "switchbot_list_devices",
        "switchbot_get_status",
        "switchbot_send_command",
    } <= tool_names
    payload = json.loads(response.json()["result"]["content"][0]["text"])
    assert "SWITCHBOT_TOKEN" in payload["error"]
    assert queue.depth == 0


@pytest.mark.asyncio
async def test_dispatcher_returns_stdio_disconnect_payload_as_tool_result() -> None:
    queue = CommandQueue(capacity=2)
    gateway = FakeGateway(connected=False)
    app = build_app(
        queue,
        gateway=gateway,
        owner_id="owner-test",
        host="127.0.0.1",
        port=8767,
        dispatch_fn=make_dispatch_fn(gateway),
    )

    async with _client(app) as client:
        session_id = await _initialize(client)
        response = await _call_tool(
            client,
            session_id=session_id,
            name="get_device_info",
        )

    payload = response.json()
    assert "error" not in payload
    result_text = payload["result"]["content"][0]["text"]
    assert json.loads(result_text) == DISCONNECTED_DEVICE_PAYLOAD


@pytest.mark.asyncio
async def test_healthz_is_liveness_only_and_status_requires_auth_for_details() -> None:
    queue = CommandQueue(capacity=2)
    app = build_app(
        queue,
        gateway=FakeGateway(),
        owner_id="owner-test",
        host="127.0.0.1",
        port=8767,
        token="secret",
    )

    async with _client(app) as client:
        health = await client.get("/healthz")
        unauthenticated_status = await client.get("/status")
        status = await client.get("/status", headers=_headers(token="secret"))

    assert health.status_code == 200
    assert health.json() == {"ok": True}
    assert set(health.json()) == {"ok"}
    assert unauthenticated_status.status_code == 401

    status_payload = status.json()
    assert status.status_code == 200
    assert status_payload["connected"] is True
    assert status_payload["esp32_connected"] is True
    assert status_payload["queue_depth"] == 0
    assert status_payload["queue_capacity"] == 2
    assert status_payload["owner_id"] == "owner-test"
    assert status_payload["connected_clients"] == 0


@pytest.mark.asyncio
async def test_command_queue_raises_queue_full_directly() -> None:
    queue = CommandQueue(capacity=1)
    loop = asyncio.get_running_loop()
    queue.enqueue(
        QueueItem(
            correlation_id="first",
            client_session_id=None,
            client_request_id=1,
            tool_name="get_device_info",
            arguments={},
            response_future=loop.create_future(),
            enqueued_at=0.0,
        )
    )
    with pytest.raises(QueueFull):
        queue.enqueue(
            QueueItem(
                correlation_id="second",
                client_session_id=None,
                client_request_id=2,
                tool_name="get_head_angles",
                arguments={},
                response_future=loop.create_future(),
                enqueued_at=0.0,
            )
        )


# ---- Phase F dashboard /control/* routes -----------------------------


class FakeHeartbeat:
    def __init__(self, *, gestures: bool = True, speak: bool = False, interval: float = 30.0):
        self._gestures = gestures
        self._speak = object() if speak else None
        self._interval_min = interval

    @property
    def gestures_enabled(self) -> bool:
        return self._gestures

    def set_gestures(self, enabled: bool) -> None:
        self._gestures = bool(enabled)


class ControlFakeESP32:
    def __init__(self, *, connected: bool = True) -> None:
        self.device_connected = connected
        self.calls: list[tuple[str, dict]] = []
        self.listen_calls: list[tuple[str, str]] = []
        self.recording = False
        # get_touch_state payload exposed to /control/status.
        self.touch_payload = {"prox_mode": "listen", "prox_threshold": 600}

    def get_status(self) -> dict:
        return {"connected": self.device_connected}

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        if name == "self.touch.get_touch_state":
            payload = self.touch_payload
        else:
            payload = {"ok": True, "name": name, "arguments": arguments}
        return {"content": [{"type": "text", "text": json.dumps(payload)}]}, None

    async def send_listen_state(self, state: str, mode: str = "manual") -> None:
        self.listen_calls.append((state, mode))


class ControlFakeGateway:
    def __init__(self, *, connected: bool = True, heartbeat: object | None = None) -> None:
        self.esp32 = ControlFakeESP32(connected=connected)
        self._heartbeat = heartbeat
        self.voice_turn_active = False


@pytest.fixture(autouse=True)
def _control_state_path(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "STACKCHAN_CONTROL_STATE", str(tmp_path / "control_state.json")
    )


def _build_control_app(gateway, *, token: str | None = None):
    return build_app(
        CommandQueue(capacity=4),
        gateway=gateway,
        owner_id="owner-test",
        host="127.0.0.1",
        port=8767,
        token=token,
    )


@pytest.mark.asyncio
async def test_control_status_connected_reports_full_payload() -> None:
    gateway = ControlFakeGateway(heartbeat=FakeHeartbeat(gestures=True, speak=True))
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.get("/control/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["esp32_connected"] is True
    assert body["volume"] == 50  # default
    assert body["muted"] is False
    assert body["mic_gain"] == 30  # default
    assert body["brightness"] == 75  # default (matches firmware NVS default)
    assert body["led"]["brightness"] == 100  # default
    assert body["led"]["idle"] == {"on": False, "r": 30, "g": 144, "b": 255}  # default
    assert body["led"]["listening"] == {"r": 0, "g": 210, "b": 90}
    assert body["led"]["hermes"] == {"r": 148, "g": 108, "b": 255}
    assert body["heartbeat"] == {"gestures": True, "speak": True, "interval_min": 30.0}
    assert body["proximity"] == {"mode": "listen", "threshold": 600}


@pytest.mark.asyncio
async def test_control_status_disconnected_nulls_device_fields() -> None:
    gateway = ControlFakeGateway(connected=False, heartbeat=None)
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.get("/control/status")
    body = resp.json()
    assert body["esp32_connected"] is False
    assert body["volume"] is None
    assert body["brightness"] is None  # live value unknown when no device
    assert body["led"]["idle"]["on"] is False  # saved preference still surfaced
    assert body["heartbeat"] is None
    assert body["proximity"] is None


@pytest.mark.asyncio
async def test_control_volume_sets_and_persists() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/volume", json={"volume": 70})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "volume": 70, "muted": False}
    assert ("self.audio_speaker.set_volume", {"volume": 70}) in gateway.esp32.calls


@pytest.mark.asyncio
async def test_control_volume_rejects_out_of_range() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/volume", json={"volume": 200})
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


@pytest.mark.asyncio
async def test_control_volume_503_when_disconnected() -> None:
    gateway = ControlFakeGateway(connected=False)
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/volume", json={"volume": 70})
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_control_brightness_sets_and_persists() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/brightness", json={"brightness": 40})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "brightness": 40}
    assert ("self.screen.set_brightness", {"brightness": 40}) in gateway.esp32.calls


@pytest.mark.asyncio
async def test_control_brightness_rejects_out_of_range() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/brightness", json={"brightness": 200})
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


@pytest.mark.asyncio
async def test_control_brightness_503_when_disconnected() -> None:
    gateway = ControlFakeGateway(connected=False)
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/brightness", json={"brightness": 40})
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_control_led_brightness_sets_and_persists() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/led_brightness", json={"brightness": 60})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "brightness": 60}


@pytest.mark.asyncio
async def test_control_led_brightness_rejects_out_of_range() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/led_brightness", json={"brightness": 200})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_control_led_idle_on_sets_all() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post(
            "/control/led", json={"slot": "idle", "on": True, "r": 10, "g": 20, "b": 30}
        )
    assert resp.status_code == 200
    assert resp.json()["led"]["idle"] == {"on": True, "r": 10, "g": 20, "b": 30}
    assert ("self.led.set_all", {"r": 10, "g": 20, "b": 30}) in gateway.esp32.calls


@pytest.mark.asyncio
async def test_control_led_idle_off_clears() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/led", json={"slot": "idle", "on": False})
    assert resp.status_code == 200
    assert ("self.led.clear", {}) in gateway.esp32.calls


@pytest.mark.asyncio
async def test_control_led_listening_persists_without_device_call() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post(
            "/control/led", json={"slot": "listening", "r": 5, "g": 6, "b": 7}
        )
    assert resp.status_code == 200
    assert resp.json()["led"]["listening"] == {"r": 5, "g": 6, "b": 7}
    assert gateway.esp32.calls == []  # listening is persisted only


@pytest.mark.asyncio
async def test_control_led_rejects_unknown_slot() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/led", json={"slot": "nope", "r": 1})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_control_led_rejects_bad_rgb() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post(
            "/control/led", json={"slot": "idle", "on": True, "r": 300, "g": 0, "b": 0}
        )
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


@pytest.mark.asyncio
async def test_control_led_idle_requires_boolean_on() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/led", json={"slot": "idle", "on": "yes"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_control_led_503_when_disconnected() -> None:
    gateway = ControlFakeGateway(connected=False)
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/led", json={"slot": "idle", "on": True})
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_control_led_test_previews_slot(monkeypatch) -> None:
    from stackchan_mcp import control

    monkeypatch.setattr(control, "LED_PREVIEW_SECONDS", 0)
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/led_test", json={"slot": "hermes"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "slot": "hermes"}
    # hermes colour shown, then revert to idle (default off -> clear).
    assert ("self.led.set_all", {"r": 148, "g": 108, "b": 255}) in gateway.esp32.calls
    assert ("self.led.clear", {}) in gateway.esp32.calls


@pytest.mark.asyncio
async def test_control_led_test_rejects_unknown_slot() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/led_test", json={"slot": "nope"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_control_mute_then_unmute() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        await client.post("/control/volume", json={"volume": 60})
        muted = await client.post("/control/mute", json={"muted": True})
        unmuted = await client.post("/control/mute", json={"muted": False})
    assert muted.json() == {"ok": True, "volume": 0, "muted": True}
    assert unmuted.json() == {"ok": True, "volume": 60, "muted": False}


@pytest.mark.asyncio
async def test_control_mute_requires_boolean() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/mute", json={"muted": "yes"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_control_listen_triggers_start(monkeypatch) -> None:
    import stackchan_mcp.audio_stream as audio_stream

    monkeypatch.setattr(audio_stream, "is_recording", lambda: False)
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/listen")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert gateway.esp32.listen_calls == [("start", "manual")]


@pytest.mark.asyncio
async def test_control_listen_already_listening_returns_409(monkeypatch) -> None:
    import stackchan_mcp.audio_stream as audio_stream

    monkeypatch.setattr(audio_stream, "is_recording", lambda: True)
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/listen")
    assert resp.status_code == 409
    assert resp.json() == {"ok": False, "error": "already listening"}


@pytest.mark.asyncio
async def test_control_proximity_dispatches() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post(
            "/control/proximity", json={"mode": "reflex", "threshold": 700}
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "mode": "reflex", "threshold": 700}
    assert (
        "self.touch.set_proximity_config",
        {"mode": "reflex", "threshold": 700},
    ) in gateway.esp32.calls


@pytest.mark.asyncio
async def test_control_proximity_validates_threshold() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post(
            "/control/proximity", json={"mode": "listen", "threshold": 9999}
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_control_proximity_validates_mode() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post(
            "/control/proximity", json={"mode": "bogus", "threshold": 600}
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_control_heartbeat_toggles_gestures() -> None:
    heartbeat = FakeHeartbeat(gestures=True)
    gateway = ControlFakeGateway(heartbeat=heartbeat)
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/heartbeat", json={"gestures": False})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "gestures": False}
    assert heartbeat.gestures_enabled is False


@pytest.mark.asyncio
async def test_control_heartbeat_503_when_no_runner() -> None:
    gateway = ControlFakeGateway(heartbeat=None)
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/heartbeat", json={"gestures": True})
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_control_avatar_dispatches() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/avatar", json={"face": "happy"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "face": "happy"}
    assert ("self.display.set_avatar", {"face": "happy"}) in gateway.esp32.calls


@pytest.mark.asyncio
async def test_control_avatar_rejects_unknown_face() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/avatar", json={"face": "angry"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_control_say_speaks(monkeypatch) -> None:
    import stackchan_mcp.tts.orchestrator as orchestrator

    seen = {}

    async def fake_send(arguments, *, gateway=None, **kw):
        seen["text"] = arguments["text"]
        return {"frame_count": 3}

    monkeypatch.setattr(orchestrator, "synthesize_and_send", fake_send)
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/say", json={"text": "こんにちは"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "tts": {"frame_count": 3}}
    assert seen["text"] == "こんにちは"


@pytest.mark.asyncio
async def test_control_say_rejects_empty_and_too_long() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        empty = await client.post("/control/say", json={"text": "   "})
        long = await client.post("/control/say", json={"text": "あ" * 201})
    assert empty.status_code == 400
    assert long.status_code == 400


@pytest.mark.asyncio
async def test_control_routes_require_token() -> None:
    gateway = ControlFakeGateway(heartbeat=FakeHeartbeat())
    app = _build_control_app(gateway, token="secret")
    async with _client(app) as client:
        missing = await client.get("/control/status")
        wrong = await client.post(
            "/control/volume",
            json={"volume": 10},
            headers=_headers(token="wrong"),
        )
        ok = await client.get("/control/status", headers=_headers(token="secret"))
    assert missing.status_code == 401
    assert missing.text == AUTH_FAILURE_MESSAGE
    assert wrong.status_code == 401
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_control_audio_level_idle(monkeypatch) -> None:
    import stackchan_mcp.audio_stream as audio_stream

    monkeypatch.setattr(audio_stream, "is_recording", lambda: False)
    monkeypatch.setattr(audio_stream, "get_input_level", lambda: 0.0)
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.get("/control/audio_level")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "recording": False, "level": 0.0}


@pytest.mark.asyncio
async def test_control_audio_level_recording(monkeypatch) -> None:
    import stackchan_mcp.audio_stream as audio_stream

    monkeypatch.setattr(audio_stream, "is_recording", lambda: True)
    monkeypatch.setattr(audio_stream, "get_input_level", lambda: 0.55)
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.get("/control/audio_level")
    body = resp.json()
    assert body["ok"] is True
    assert body["recording"] is True
    assert body["level"] == 0.55


@pytest.mark.asyncio
async def test_control_audio_level_requires_token() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway, token="secret")
    async with _client(app) as client:
        missing = await client.get("/control/audio_level")
        ok = await client.get(
            "/control/audio_level", headers=_headers(token="secret")
        )
    assert missing.status_code == 401
    assert ok.status_code == 200


# ---- mic gain ---------------------------------------------------------


@pytest.mark.asyncio
async def test_control_mic_gain_sets_and_persists() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/mic_gain", json={"gain": 24})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "gain": 24, "connected": True}
    assert (
        "self.audio_speaker.set_mic_gain",
        {"gain": 24},
    ) in gateway.esp32.calls


@pytest.mark.asyncio
async def test_control_mic_gain_reflected_in_status() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        await client.post("/control/mic_gain", json={"gain": 12})
        status = await client.get("/control/status")
    assert status.json()["mic_gain"] == 12


@pytest.mark.asyncio
async def test_control_mic_gain_rejects_out_of_range() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        too_high = await client.post("/control/mic_gain", json={"gain": 37})
        negative = await client.post("/control/mic_gain", json={"gain": -1})
    assert too_high.status_code == 400
    assert too_high.json()["ok"] is False
    assert negative.status_code == 400


@pytest.mark.asyncio
async def test_control_mic_gain_503_when_disconnected() -> None:
    gateway = ControlFakeGateway(connected=False)
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/mic_gain", json={"gain": 20})
    assert resp.status_code == 503


# ---- /control/head ----------------------------------------------------


@pytest.mark.asyncio
async def test_control_head_moves_live() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/head", json={"yaw": 25, "pitch": 55})
    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True,
        "yaw": 25,
        "pitch": 55,
        "connected": True,
    }
    assert (
        "self.robot.set_head_angles",
        {"yaw": 25, "pitch": 55},
    ) in gateway.esp32.calls


@pytest.mark.asyncio
async def test_control_head_rejects_out_of_range() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        bad_yaw = await client.post("/control/head", json={"yaw": 200, "pitch": 30})
        bad_pitch = await client.post("/control/head", json={"yaw": 0, "pitch": 1})
    assert bad_yaw.status_code == 400
    assert bad_yaw.json()["ok"] is False
    assert bad_pitch.status_code == 400


@pytest.mark.asyncio
async def test_control_head_503_when_disconnected() -> None:
    gateway = ControlFakeGateway(connected=False)
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post("/control/head", json={"yaw": 0, "pitch": 30})
    assert resp.status_code == 503


# ---- /control/neutral_pose --------------------------------------------


@pytest.mark.asyncio
async def test_control_neutral_pose_persists() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post(
            "/control/neutral_pose", json={"yaw": -10, "pitch": 40}
        )
    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True,
        "yaw": -10,
        "pitch": 40,
        "connected": True,
    }
    assert (
        "self.robot.set_neutral_pose",
        {"yaw": -10, "pitch": 40},
    ) in gateway.esp32.calls


@pytest.mark.asyncio
async def test_control_neutral_pose_rejects_out_of_range() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post(
            "/control/neutral_pose", json={"yaw": 0, "pitch": 999}
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_control_neutral_pose_503_when_disconnected() -> None:
    gateway = ControlFakeGateway(connected=False)
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.post(
            "/control/neutral_pose", json={"yaw": 0, "pitch": 30}
        )
    assert resp.status_code == 503


# ---- conversation log -------------------------------------------------


@pytest.mark.asyncio
async def test_control_conversation_empty() -> None:
    from stackchan_mcp import control

    control._CONVERSATION.clear()
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.get("/control/conversation")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "turns": []}


@pytest.mark.asyncio
async def test_control_conversation_returns_recorded_turns() -> None:
    from stackchan_mcp import control

    control._CONVERSATION.clear()
    control.record_conversation_turn("おはよう", "おはよう！", "local", {"total": 480})
    control.record_conversation_turn("天気は？", "晴れです", "hermes", {"total": 1500})
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway)
    async with _client(app) as client:
        resp = await client.get("/control/conversation")
    body = resp.json()
    assert body["ok"] is True
    assert [t["transcript"] for t in body["turns"]] == ["おはよう", "天気は？"]
    assert body["turns"][0]["route"] == "local"
    assert body["turns"][1]["timings_ms"] == {"total": 1500}
    control._CONVERSATION.clear()


@pytest.mark.asyncio
async def test_control_conversation_requires_token() -> None:
    gateway = ControlFakeGateway()
    app = _build_control_app(gateway, token="secret")
    async with _client(app) as client:
        missing = await client.get("/control/conversation")
        ok = await client.get(
            "/control/conversation", headers=_headers(token="secret")
        )
    assert missing.status_code == 401
    assert ok.status_code == 200
