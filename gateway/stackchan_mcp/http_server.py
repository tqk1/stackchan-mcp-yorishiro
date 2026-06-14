"""Streamable HTTP MCP daemon wiring for the StackChan gateway."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

import jsonschema
from mcp.server.streamable_http import MCP_SESSION_ID_HEADER
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolRequest, CallToolResult, ErrorData, ServerResult, TextContent
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from . import control
from .notes import TOOL_NAMES as NOTES_TOOL_NAMES
from .notify_config import NotifyConfig
from .queue import CommandQueue, QueueFull, QueueItem, build_queue_full_error
from .stdio_server import _dispatch_mcp_tool, create_server
from .switchbot import TOOL_NAMES as SWITCHBOT_TOOL_NAMES
from .web_search import TOOL_NAMES as WEB_SEARCH_TOOL_NAMES

# Tools handled gateway-locally (no ESP32 round-trip): they bypass the
# single-flight device queue and its device_connected guard.
BYPASS_TOOLS = (
    frozenset({"get_status"})
    | SWITCHBOT_TOOL_NAMES
    | WEB_SEARCH_TOOL_NAMES
    | NOTES_TOOL_NAMES
)
MCP_HTTP_ALLOWED_HOSTS_ENV = "MCP_HTTP_ALLOWED_HOSTS"
AUTH_FAILURE_MESSAGE = "Unauthorized: missing or invalid bearer token"
HOST_FAILURE_MESSAGE = "Forbidden: invalid Host header"
ORIGIN_FAILURE_MESSAGE = "Forbidden: invalid Origin header"
NON_LOOPBACK_TOKEN_REQUIRED_MESSAGE = (
    "stackchan-mcp: refusing non-loopback MCP_HTTP_HOST without "
    "STACKCHAN_TOKEN or BEARER_TOKEN"
)
DISCONNECTED_DEVICE_PAYLOAD = {
    "error": "No ESP32 device connected. Please check the device."
}
#: Path prefix for the Phase F dashboard control routes. Token-guarded
#: alongside /mcp and /status (see _GuardedASGIApp.__call__).
CONTROL_PATH_PREFIX = "/control"
#: Avatar faces accepted by POST /control/avatar (mirrors the firmware
#: AvatarSet faces plus "off").
CONTROL_AVATAR_FACES = frozenset(
    {"idle", "happy", "thinking", "sad", "surprised", "embarrassed", "off"}
)
#: Upper bound on POST /control/say text (one spoken breath on a 1 W
#: speaker; longer monologues kill the rhythm).
CONTROL_SAY_MAX_CHARS = 200
SERVER_SHUTDOWN_ERROR_CODE = -32000
SERVER_SHUTDOWN_ERROR_MESSAGE = "stackchan MCP HTTP server is shutting down"

DispatchFn = Callable[[QueueItem], Awaitable[list[TextContent]]]


def get_configured_token() -> str | None:
    """Return the configured HTTP bearer token, if any."""
    return os.getenv("STACKCHAN_TOKEN") or os.getenv("BEARER_TOKEN") or None


def is_wildcard_bind_host(host: str) -> bool:
    """Return whether ``host`` binds all local interfaces."""
    normalized = host.strip().lower()
    return normalized in {"", "0.0.0.0", "::"}


def is_loopback_bind_host(host: str) -> bool:
    """Return whether ``host`` is a loopback-only bind target."""
    normalized = host.strip().lower()
    if normalized in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_bind_safety(host: str, token: str | None) -> None:
    """Reject non-loopback daemon binds when no HTTP bearer token is set."""
    if not token and not is_loopback_bind_host(host):
        raise ValueError(NON_LOOPBACK_TOKEN_REQUIRED_MESSAGE)


def make_dispatch_fn(gateway: Any) -> DispatchFn:
    """Build the single-flight ESP32 dispatcher used by the command queue."""

    async def dispatch(item: QueueItem) -> list[TextContent]:
        if not gateway.esp32.device_connected:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(DISCONNECTED_DEVICE_PAYLOAD),
                )
            ]
        return await _dispatch_mcp_tool(item.tool_name, item.arguments, gateway)

    return dispatch


# ---- Phase F dashboard control helpers --------------------------------


async def _read_json_body(request: Request) -> dict[str, Any]:
    """Best-effort JSON body as a dict ({} for empty / non-object)."""
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _control_json(payload: dict[str, Any], *, status: int = 200) -> JSONResponse:
    code = status
    if not payload.get("ok", True) and status == 200:
        # A device-call failure without an explicit status maps to 502.
        code = 502
    return JSONResponse(payload, status_code=code)


def _control_error(message: str, *, status: int) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def _device_tool_payload(content: list[Any]) -> Any:
    """Extract the JSON payload (or raw text) from an ESP32 tool result.

    The device tools come back as a list of ``TextContent``; the first
    text item is usually a JSON document (e.g. get_touch_state) but can
    also be a plain string. Returns a dict when parseable, the raw
    string otherwise, or None when there is no text content.
    """
    for item in content:
        text = getattr(item, "text", None)
        if text is None and isinstance(item, dict):
            text = item.get("text")
        if not isinstance(text, str):
            continue
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text
    return None


def _content_has_error(content: list[Any]) -> str | None:
    """Return the error string when a device tool result carried one."""
    payload = _device_tool_payload(content)
    if isinstance(payload, dict) and "error" in payload:
        return str(payload["error"])
    return None


def _control_device_result(
    content: list[Any], **extra: Any
) -> JSONResponse:
    """Map a device tool dispatch result to a control JSON response."""
    error = _content_has_error(content)
    if error is not None:
        return _control_error(error, status=502)
    return _control_json({"ok": True, **extra})


async def _build_control_status(gateway: Any) -> dict[str, Any]:
    """Assemble the GET /control/status payload (REST contract)."""
    connected = bool(gateway.esp32.device_connected)
    state = control.load_state()
    volume: int | None = state["volume"] if connected else None
    muted = state["muted"]
    mic_gain = state["mic_gain"]
    # Brightness is a live device value (mirrors volume: unknown when no
    # device). The LED block is a saved preference, so it is always
    # surfaced — the toggle shows what will be applied on (re)connect.
    brightness: int | None = state["brightness"] if connected else None
    led = state["led"]

    heartbeat = _heartbeat_status(gateway)
    proximity = await _proximity_status(gateway) if connected else None

    return {
        "ok": True,
        "esp32_connected": connected,
        "volume": volume,
        "muted": muted,
        "mic_gain": mic_gain,
        "brightness": brightness,
        "led": led,
        "heartbeat": heartbeat,
        "proximity": proximity,
    }


def _heartbeat_status(gateway: Any) -> dict[str, Any] | None:
    runner = getattr(gateway, "_heartbeat", None)
    if runner is None:
        return None
    return {
        "gestures": bool(runner.gestures_enabled),
        "speak": runner._speak is not None,
        "interval_min": runner._interval_min,
    }


async def _proximity_status(gateway: Any) -> dict[str, Any] | None:
    content = await _dispatch_mcp_tool("get_touch_state", {}, gateway)
    payload = _device_tool_payload(content)
    if not isinstance(payload, dict):
        return None
    mode = payload.get("prox_mode")
    threshold = payload.get("prox_threshold")
    if mode not in ("reflex", "listen", "off") or not isinstance(threshold, int):
        return None
    return {"mode": mode, "threshold": threshold}


def build_app(
    queue: CommandQueue,
    *,
    gateway: Any,
    owner_id: str,
    host: str,
    port: int,
    token: str | None = None,
    dispatch_fn: DispatchFn | None = None,
    notify_config: NotifyConfig | None = None,
) -> _GuardedASGIApp:
    """Build the ASGI app for Streamable HTTP MCP plus health endpoints."""
    server = create_server(notify_config=notify_config)
    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=True,
        stateless=False,
    )
    pending_items: dict[str, QueueItem] = {}
    _install_queue_tool_handler(
        server,
        queue=queue,
        gateway=gateway,
        pending_items=pending_items,
    )

    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def status(_request: Request) -> JSONResponse:
        raw_status = gateway.esp32.get_status()
        status_payload = dict(raw_status) if isinstance(raw_status, dict) else {}
        if not isinstance(raw_status, dict):
            status_payload["status"] = raw_status
        status_payload.update(
            {
                "esp32_connected": bool(gateway.esp32.device_connected),
                "queue_depth": queue.depth,
                "queue_capacity": queue.capacity,
                "owner_id": owner_id,
                "connected_clients": _connected_client_count(session_manager),
            }
        )
        return JSONResponse(status_payload)

    async def control_status(_request: Request) -> JSONResponse:
        return JSONResponse(await _build_control_status(gateway))

    async def control_audio_level(_request: Request) -> JSONResponse:
        # Gateway-local read of the live mic level; no device round-trip,
        # so this works even mid-capture without contending the queue.
        return JSONResponse(control.get_audio_level())

    async def control_conversation(_request: Request) -> JSONResponse:
        # Gateway-local read of the rolling conversation log; no device
        # round-trip, so it never contends the command queue.
        return JSONResponse(control.get_conversation())

    async def control_volume(request: Request) -> JSONResponse:
        body = await _read_json_body(request)
        if not gateway.esp32.device_connected:
            return _control_error("no device connected", status=503)
        volume = body.get("volume")
        if not isinstance(volume, int) or isinstance(volume, bool) or not 0 <= volume <= 100:
            return _control_error("volume must be an integer 0..100", status=400)
        return _control_json(await control.set_volume(gateway, volume))

    async def control_mic_gain(request: Request) -> JSONResponse:
        body = await _read_json_body(request)
        connected = bool(gateway.esp32.device_connected)
        if not connected:
            return _control_error("no device connected", status=503)
        gain = body.get("gain")
        if not isinstance(gain, int) or isinstance(gain, bool) or not 0 <= gain <= 36:
            return _control_error("gain must be an integer 0..36", status=400)
        result = await control.set_mic_gain(gateway, gain)
        return _control_json({**result, "connected": connected})

    async def control_brightness(request: Request) -> JSONResponse:
        body = await _read_json_body(request)
        if not gateway.esp32.device_connected:
            return _control_error("no device connected", status=503)
        brightness = body.get("brightness")
        if (
            not isinstance(brightness, int)
            or isinstance(brightness, bool)
            or not 0 <= brightness <= 100
        ):
            return _control_error(
                "brightness must be an integer 0..100", status=400
            )
        return _control_json(await control.set_brightness(gateway, brightness))

    async def control_led(request: Request) -> JSONResponse:
        body = await _read_json_body(request)
        if not gateway.esp32.device_connected:
            return _control_error("no device connected", status=503)
        slot = body.get("slot")
        if slot not in control.LED_SLOTS:
            return _control_error(
                f"slot must be one of {list(control.LED_SLOTS)}", status=400
            )
        rgb = {}
        for key in ("r", "g", "b"):
            val = body.get(key, 0)
            if not isinstance(val, int) or isinstance(val, bool) or not 0 <= val <= 255:
                return _control_error(f"{key} must be an integer 0..255", status=400)
            rgb[key] = val
        on = body.get("on")
        if slot == "idle" and not isinstance(on, bool):
            return _control_error("on must be a boolean for the idle slot", status=400)
        return _control_json(await control.set_led(gateway, slot, on=on, **rgb))

    async def control_led_test(request: Request) -> JSONResponse:
        body = await _read_json_body(request)
        if not gateway.esp32.device_connected:
            return _control_error("no device connected", status=503)
        slot = body.get("slot")
        if slot not in control.LED_SLOTS:
            return _control_error(
                f"slot must be one of {list(control.LED_SLOTS)}", status=400
            )
        return _control_json(await control.preview_led(gateway, slot))

    async def control_led_brightness(request: Request) -> JSONResponse:
        body = await _read_json_body(request)
        if not gateway.esp32.device_connected:
            return _control_error("no device connected", status=503)
        brightness = body.get("brightness")
        if (
            not isinstance(brightness, int)
            or isinstance(brightness, bool)
            or not 0 <= brightness <= 100
        ):
            return _control_error(
                "brightness must be an integer 0..100", status=400
            )
        return _control_json(await control.set_led_brightness(gateway, brightness))

    async def control_head(request: Request) -> JSONResponse:
        body = await _read_json_body(request)
        if not gateway.esp32.device_connected:
            return _control_error("no device connected", status=503)
        yaw = body.get("yaw")
        pitch = body.get("pitch")
        if not isinstance(yaw, int) or isinstance(yaw, bool) or not -90 <= yaw <= 90:
            return _control_error("yaw must be an integer -90..90", status=400)
        if (
            not isinstance(pitch, int)
            or isinstance(pitch, bool)
            or not 5 <= pitch <= 85
        ):
            return _control_error("pitch must be an integer 5..85", status=400)
        return _control_json(await control.set_head_angle(gateway, yaw, pitch))

    async def control_neutral_pose(request: Request) -> JSONResponse:
        body = await _read_json_body(request)
        if not gateway.esp32.device_connected:
            return _control_error("no device connected", status=503)
        yaw = body.get("yaw")
        pitch = body.get("pitch")
        if not isinstance(yaw, int) or isinstance(yaw, bool) or not -90 <= yaw <= 90:
            return _control_error("yaw must be an integer -90..90", status=400)
        if (
            not isinstance(pitch, int)
            or isinstance(pitch, bool)
            or not 5 <= pitch <= 85
        ):
            return _control_error("pitch must be an integer 5..85", status=400)
        return _control_json(await control.set_neutral_pose(gateway, yaw, pitch))

    async def control_mute(request: Request) -> JSONResponse:
        body = await _read_json_body(request)
        if not gateway.esp32.device_connected:
            return _control_error("no device connected", status=503)
        muted = body.get("muted")
        if not isinstance(muted, bool):
            return _control_error("muted must be a boolean", status=400)
        result = await (control.mute(gateway) if muted else control.unmute(gateway))
        return _control_json(result)

    async def control_listen(_request: Request) -> JSONResponse:
        if not gateway.esp32.device_connected:
            return _control_error("no device connected", status=503)
        result = await control.trigger_listen(gateway)
        if not result.get("ok") and result.get("error") == "already listening":
            return _control_json(result, status=409)
        return _control_json(result)

    async def control_proximity(request: Request) -> JSONResponse:
        body = await _read_json_body(request)
        if not gateway.esp32.device_connected:
            return _control_error("no device connected", status=503)
        mode = body.get("mode")
        threshold = body.get("threshold")
        if mode not in ("reflex", "listen", "off"):
            return _control_error(
                "mode must be one of: reflex, listen, off", status=400
            )
        if (
            not isinstance(threshold, int)
            or isinstance(threshold, bool)
            or not 0 <= threshold <= 2047
        ):
            return _control_error("threshold must be an integer 0..2047", status=400)
        content = await _dispatch_mcp_tool(
            "set_proximity_config",
            {"mode": mode, "threshold": threshold},
            gateway,
        )
        return _control_device_result(content, mode=mode, threshold=threshold)

    async def control_heartbeat(request: Request) -> JSONResponse:
        body = await _read_json_body(request)
        runner = getattr(gateway, "_heartbeat", None)
        if runner is None:
            return _control_error("heartbeat not running", status=503)
        gestures = body.get("gestures")
        if not isinstance(gestures, bool):
            return _control_error("gestures must be a boolean", status=400)
        runner.set_gestures(gestures)
        return _control_json({"ok": True, "gestures": runner.gestures_enabled})

    async def control_avatar(request: Request) -> JSONResponse:
        body = await _read_json_body(request)
        if not gateway.esp32.device_connected:
            return _control_error("no device connected", status=503)
        face = body.get("face")
        if face not in CONTROL_AVATAR_FACES:
            return _control_error(
                f"face must be one of {sorted(CONTROL_AVATAR_FACES)}", status=400
            )
        content = await _dispatch_mcp_tool("set_avatar", {"face": face}, gateway)
        return _control_device_result(content, face=face)

    async def control_say(request: Request) -> JSONResponse:
        body = await _read_json_body(request)
        if not gateway.esp32.device_connected:
            return _control_error("no device connected", status=503)
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return _control_error("text must be a non-empty string", status=400)
        if len(text) > CONTROL_SAY_MAX_CHARS:
            return _control_error(
                f"text exceeds {CONTROL_SAY_MAX_CHARS} characters", status=400
            )
        from .tts.orchestrator import synthesize_and_send

        try:
            result = await synthesize_and_send({"text": text}, gateway=gateway)
        except (ValueError, NotImplementedError, RuntimeError, ConnectionError) as exc:
            return _control_error(f"say failed: {exc}", status=502)
        return _control_json({"ok": True, "tts": result})

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        dispatcher_task: asyncio.Task[None] | None = None
        async with session_manager.run():
            if dispatch_fn is not None:
                dispatcher_task = asyncio.create_task(
                    queue.run_dispatcher(_skip_done_dispatch(dispatch_fn))
                )
            try:
                yield
            finally:
                if dispatcher_task is not None:
                    dispatcher_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await dispatcher_task
                _complete_pending_items_for_shutdown(pending_items)
                _drain_queued_items_for_shutdown(queue)

    routes = [
        Route(
            "/mcp",
            endpoint=_StreamableHTTPASGIApp(session_manager),
            methods=["GET", "POST", "DELETE"],
        ),
        Route("/healthz", endpoint=healthz, methods=["GET"]),
        Route("/status", endpoint=status, methods=["GET"]),
        # Phase F dashboard control routes (token-guarded by prefix).
        Route("/control/status", endpoint=control_status, methods=["GET"]),
        Route("/control/audio_level", endpoint=control_audio_level, methods=["GET"]),
        Route("/control/conversation", endpoint=control_conversation, methods=["GET"]),
        Route("/control/volume", endpoint=control_volume, methods=["POST"]),
        Route("/control/mic_gain", endpoint=control_mic_gain, methods=["POST"]),
        Route("/control/brightness", endpoint=control_brightness, methods=["POST"]),
        Route("/control/led", endpoint=control_led, methods=["POST"]),
        Route("/control/led_test", endpoint=control_led_test, methods=["POST"]),
        Route(
            "/control/led_brightness",
            endpoint=control_led_brightness,
            methods=["POST"],
        ),
        Route("/control/head", endpoint=control_head, methods=["POST"]),
        Route(
            "/control/neutral_pose",
            endpoint=control_neutral_pose,
            methods=["POST"],
        ),
        Route("/control/mute", endpoint=control_mute, methods=["POST"]),
        Route("/control/listen", endpoint=control_listen, methods=["POST"]),
        Route("/control/proximity", endpoint=control_proximity, methods=["POST"]),
        Route("/control/heartbeat", endpoint=control_heartbeat, methods=["POST"]),
        Route("/control/avatar", endpoint=control_avatar, methods=["POST"]),
        Route("/control/say", endpoint=control_say, methods=["POST"]),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.command_queue = queue
    app.state.session_manager = session_manager
    app.state.gateway = gateway
    return _GuardedASGIApp(
        app,
        token=token,
        allowed_hosts=_allowed_host_values(host, port),
    )


def _install_queue_tool_handler(
    server: Any,
    *,
    queue: CommandQueue,
    gateway: Any,
    pending_items: dict[str, QueueItem],
) -> None:
    async def handler(req: CallToolRequest) -> ServerResult | ErrorData:
        tool_name = req.params.name
        arguments = req.params.arguments or {}
        tool = await server._get_cached_tool_definition(tool_name)
        if tool is not None:
            try:
                jsonschema.validate(instance=arguments, schema=tool.inputSchema)
            except jsonschema.ValidationError as exc:
                return server._make_error_result(
                    f"Input validation error: {exc.message}"
                )

        if tool_name in BYPASS_TOOLS:
            content = await _dispatch_mcp_tool(tool_name, arguments, gateway)
            return _tool_result(content)

        context = server.request_context
        request = context.request
        client_session_id = None
        if isinstance(request, Request):
            client_session_id = request.headers.get(MCP_SESSION_ID_HEADER)

        response_future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        item = QueueItem(
            correlation_id=str(uuid.uuid4()),
            client_session_id=client_session_id,
            client_request_id=context.request_id,
            tool_name=tool_name,
            arguments=arguments,
            response_future=response_future,
            enqueued_at=time.monotonic(),
        )
        try:
            queue.enqueue(item)
        except QueueFull as exc:
            return ErrorData(**build_queue_full_error(exc.queue_depth))

        pending_items[item.correlation_id] = item
        try:
            content_or_error = await response_future
        except asyncio.CancelledError:
            response_future.cancel()
            raise
        finally:
            if response_future.done():
                pending_items.pop(item.correlation_id, None)

        if isinstance(content_or_error, ErrorData):
            return content_or_error
        return _tool_result(content_or_error)

    server.request_handlers[CallToolRequest] = handler


def _skip_done_dispatch(dispatch_fn: DispatchFn) -> DispatchFn:
    async def dispatch(item: QueueItem) -> list[TextContent]:
        if item.response_future.done():
            return []
        return await dispatch_fn(item)

    return dispatch


def _complete_pending_items_for_shutdown(
    pending_items: dict[str, QueueItem],
) -> None:
    for item in list(pending_items.values()):
        _complete_item_with_shutdown_error(item)
    pending_items.clear()


def _drain_queued_items_for_shutdown(queue: CommandQueue) -> None:
    raw_queue = getattr(queue, "_queue")
    while True:
        try:
            item = raw_queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        _complete_item_with_shutdown_error(item)
        raw_queue.task_done()


def _complete_item_with_shutdown_error(item: QueueItem) -> None:
    if not item.response_future.done():
        item.response_future.set_result(_server_shutdown_error())


def _server_shutdown_error() -> ErrorData:
    return ErrorData(
        code=SERVER_SHUTDOWN_ERROR_CODE,
        message=SERVER_SHUTDOWN_ERROR_MESSAGE,
        data={"reason": "server_shutdown"},
    )


def _tool_result(content: list[TextContent]) -> ServerResult:
    return ServerResult(
        CallToolResult(
            content=content,
            isError=False,
        )
    )


def _connected_client_count(session_manager: StreamableHTTPSessionManager) -> int:
    return len(getattr(session_manager, "_server_instances", {}))


def _allowed_host_values(host: str, port: int) -> set[str]:
    hosts = {host.strip().lower()}
    if is_loopback_bind_host(host) or is_wildcard_bind_host(host):
        hosts.update({"127.0.0.1", "localhost", "::1"})

    values: set[str] = set()
    for item in hosts:
        values.add(item)
        values.add(_host_with_port(item, port))
    values.update(_allowed_hosts_from_env(port))
    return values


def _allowed_hosts_from_env(port: int) -> set[str]:
    raw_hosts = os.getenv(MCP_HTTP_ALLOWED_HOSTS_ENV, "")
    values: set[str] = set()
    for raw_item in raw_hosts.split(","):
        item = raw_item.strip().lower()
        if not item:
            continue
        parsed = urlparse(item)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            item = parsed.netloc.lower()
        values.add(item)
        if ":" not in item or (item.startswith("[") and "]:" not in item):
            values.add(_host_with_port(item, port))
    return values


def _host_with_port(host: str, port: int) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def _is_allowed_host_header(value: str | None, allowed_hosts: set[str]) -> bool:
    if not value:
        return False
    return value.strip().lower() in allowed_hosts


def _is_allowed_origin(value: str | None, allowed_hosts: set[str]) -> bool:
    if not value:
        return True
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    return _is_allowed_host_header(parsed.netloc, allowed_hosts)


class _GuardedASGIApp:
    def __init__(
        self,
        app: Starlette,
        *,
        token: str | None,
        allowed_hosts: set[str],
    ) -> None:
        self._app = app
        self._token = token
        self._allowed_hosts = allowed_hosts
        self.state = app.state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request = Request(scope, receive)
        if not _is_allowed_host_header(request.headers.get("host"), self._allowed_hosts):
            await PlainTextResponse(HOST_FAILURE_MESSAGE, status_code=403)(
                scope,
                receive,
                send,
            )
            return
        if not _is_allowed_origin(request.headers.get("origin"), self._allowed_hosts):
            await PlainTextResponse(ORIGIN_FAILURE_MESSAGE, status_code=403)(
                scope,
                receive,
                send,
            )
            return
        path = scope.get("path", "")
        token_protected = path in {"/mcp", "/status"} or path.startswith(
            CONTROL_PATH_PREFIX
        )
        if self._token and token_protected:
            expected = f"Bearer {self._token}"
            if request.headers.get("authorization") != expected:
                await PlainTextResponse(
                    AUTH_FAILURE_MESSAGE,
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )(scope, receive, send)
                return

        await self._app(scope, receive, send)

    async def router_startup(self) -> None:
        await self._app.router.startup()

    @property
    def router(self) -> Any:
        return self._app.router


class _StreamableHTTPASGIApp:
    def __init__(self, session_manager: StreamableHTTPSessionManager) -> None:
        self._session_manager = session_manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._session_manager.handle_request(scope, receive, send)
