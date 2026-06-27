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
from pathlib import Path
from typing import Any, Final
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

from . import activity_log, control, local_llm, sensors
from .notes import TOOL_NAMES as NOTES_TOOL_NAMES
from .notify_config import NotifyConfig
from .queue import CommandQueue, QueueFull, QueueItem, build_queue_full_error
from .stdio_server import _dispatch_mcp_tool, create_server
from .switchbot import TOOL_NAMES as SWITCHBOT_TOOL_NAMES
from .web_search import TOOL_NAMES as WEB_SEARCH_TOOL_NAMES

# Tools handled gateway-locally (no ESP32 round-trip): they bypass the
# single-flight device queue and its device_connected guard.
BYPASS_TOOLS = (
    frozenset({"get_status", "get_presence"})
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


def _control_i2c_result(content: list[Any]) -> JSONResponse:
    """Map an ``i2c_*`` device dispatch to a control JSON response.

    Surfaces the device payload verbatim (e.g. ``{"bytes": [...]}`` for a
    read, or the scan's address list) so dashboard / probe scripts get the
    raw values back instead of a flattened ``ok``.
    """
    error = _content_has_error(content)
    if error is not None:
        return _control_error(error, status=502)
    payload = _device_tool_payload(content)
    if payload is None:
        return _control_error("empty device response", status=502)
    if isinstance(payload, dict):
        return _control_json({"ok": True, **payload})
    return _control_json({"ok": True, "result": payload})


def _i2c_byte_list(value: Any) -> list[int] | None:
    """Validate a JSON array as I2C bytes (each 0..255); None if invalid."""
    if not isinstance(value, list) or not value:
        return None
    out: list[int] = []
    for b in value:
        if not isinstance(b, int) or isinstance(b, bool) or not 0 <= b <= 255:
            return None
        out.append(b)
    return out


def _i2c_n_bytes(value: Any) -> int | None:
    """Validate an I2C read length (1..256); None if invalid."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= 256 else None


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
    monitor = getattr(gateway, "_presence", None)
    presence = monitor.snapshot() if monitor is not None else {"enabled": False}

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
        "presence": presence,
        "routing": {
            "force_hermes": state["force_hermes"],
            "local_enabled": local_llm.is_enabled(),
            "multiturn": state["multiturn"],
        },
        "proactive": {
            # ``enabled`` is the persisted dashboard toggle (runtime truth);
            # ``available`` is whether the speaker was built at all (the
            # STACKCHAN_PROACTIVE env master switch). Toggling has no effect
            # until available is true.
            "enabled": state["proactive_enabled"],
            "available": getattr(gateway, "_proactive", None) is not None,
        },
    }


async def _build_preset_snapshot(gateway: Any) -> dict[str, Any]:
    """Snapshot the current dashboard-controllable settings for a preset.

    Reuses the same sources as :func:`_build_control_status`: the gateway
    control state plus the device-queried proximity and the heartbeat
    runner. The head's neutral pose is intentionally excluded (it depends
    on where the device is placed). ``control.save_preset`` sanitises this.
    """
    state = control.load_state()
    snapshot: dict[str, Any] = {
        "volume": state["volume"],
        "muted": state["muted"],
        "pre_mute_volume": state["pre_mute_volume"],
        "mic_gain": state["mic_gain"],
        "brightness": state["brightness"],
        "led": state["led"],
    }
    proximity = await _proximity_status(gateway)
    if proximity is not None:
        snapshot["proximity"] = proximity
    heartbeat = _heartbeat_status(gateway)
    if heartbeat is not None and "gestures" in heartbeat:
        snapshot["heartbeat"] = {"gestures": heartbeat["gestures"]}
    return snapshot


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


def _parse_days(value: Any, *, default: int = 7, lo: int = 1, hi: int = 28) -> int:
    """Clamp a ``?days=`` query value to [lo, hi]; malformed -> default."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(n, lo), hi)


def _parse_limit(value: Any, *, default: int = 80, lo: int = 1, hi: int = 200) -> int:
    """Clamp a ``?limit=`` query value to [lo, hi]; malformed -> default."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(n, lo), hi)


# --- activity feed (yorishiro): autonomous-activity JSONL + daily reports ---

#: JSONL-backed activity sources (written by activity_log.append). ``report``
#: items are merged in from the daily presence reports, not the JSONL.
ACTIVITY_JSONL_SOURCES = frozenset({"heartbeat", "proactive", "presence", "home"})
PRESENCE_REPORT_ENV = "STACKCHAN_PRESENCE_REPORT"

#: Deep-night Obsidian-vault housekeeping crons surfaced in the feed (yorishiro),
#: as ``(log path, label)``. Each job's log mtime is its last-run time; we emit
#: one item per job. The hourly Claude-Code token-usage cron (``fetch_usage.py``
#: → ``~/razer-dashboard/fetch_usage.log``) is deliberately absent: per-hour
#: entries flood the feed and duplicate the server tab's CC usage (Kenji's call,
#: 2026-06-27), so leaving it out keeps it hidden.
CRON_JOBS: Final[tuple[tuple[str, str], ...]] = (
    ("/tmp/inbox-drain.log", "Inbox 整理"),
    ("/tmp/build_moc.log", "MOC 構築"),
    ("/tmp/build_notes_review.log", "ノートレビュー更新"),
    ("/tmp/weekly-digest.log", "週次ダイジェスト"),
    ("/tmp/notes-tidy.log", "ノート整理"),
    ("/tmp/notes-tidy-suggest.log", "整理提案"),
    ("/tmp/articles-recommend.log", "記事レコメンド"),
    ("/tmp/self-reflect.log", "自己ふりかえり"),
)
#: Lower-cased substrings in a cron log's last line that mark a failed run.
_CRON_ERROR_MARKERS: Final = (
    "error",
    "traceback",
    "permission denied",
    "exception",
    "failed",
    "not found",
    "no such file",
)


def _presence_report_dir() -> Path:
    raw = os.environ.get(PRESENCE_REPORT_ENV)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".stackchan" / "presence_reports"


def _list_presence_reports(limit: int, *, path: Path | None = None) -> list[dict[str, Any]]:
    """List the most recent daily presence reports as feed link items."""
    if path is None:
        path = _presence_report_dir()
    if limit <= 0 or not path.exists() or not path.is_dir():
        return []
    try:
        files = sorted(path.glob("*.json"))
    except OSError:
        return []
    items: list[dict[str, Any]] = []
    for f in files[-limit:]:
        date = f.stem  # YYYY-MM-DD
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        items.append(
            {
                "ts_unix": mtime,
                "source": "report",
                "kind": "daily_report",
                "status": "ok",
                "subtype": date,
                "text": f"{date} の在室日次レポート",
                "detail": {"date": date},
            }
        )
    return items


def _last_log_line(path: Path, *, max_bytes: int = 4096) -> str:
    """Best-effort last non-empty line of a log, read from the tail (≤200 chars)."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            tail = f.read()
    except OSError:
        return ""
    for line in reversed(tail.decode("utf-8", "replace").splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped[:200]
    return ""


def _read_cron_runs(
    jobs: tuple[tuple[str, str], ...] | None = None,
) -> list[dict[str, Any]]:
    """Surface deep-night Obsidian housekeeping crons in the feed.

    Each job's log gives its last-run time (file mtime) plus a one-line tail
    for context; we emit one ``source="cron"`` item per job (its most recent
    run). Read-only and best-effort: a missing log — e.g. cleared on reboot
    until the job next runs — simply yields nothing for that job. The hourly
    token-usage cron is intentionally not in ``jobs``, so it stays hidden.

    ``jobs`` defaults to the module-level :data:`CRON_JOBS` (resolved at call
    time so tests can monkeypatch it).
    """
    items: list[dict[str, Any]] = []
    for path_str, label in CRON_JOBS if jobs is None else jobs:
        path = Path(path_str)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        text = _last_log_line(path)
        status = (
            "error"
            if text and any(m in text.lower() for m in _CRON_ERROR_MARKERS)
            else "ok"
        )
        items.append(
            {
                "ts_unix": mtime,
                "source": "cron",
                "kind": "run",
                "subtype": label,
                "status": status,
                "text": text,
            }
        )
    return items


def _gather_activity(limit: int, source: str | None) -> list[dict[str, Any]]:
    """Merge autonomous-activity JSONL + cron runs + daily reports, newest first."""
    items: list[dict[str, Any]] = []
    if source is None or source in ACTIVITY_JSONL_SOURCES:
        items += activity_log.read_recent(
            limit, source=source if source in ACTIVITY_JSONL_SOURCES else None
        )
    if source is None or source == "cron":
        items += _read_cron_runs()
    if source is None or source == "report":
        items += _list_presence_reports(14)
    items.sort(key=lambda r: r.get("ts_unix", 0.0), reverse=True)
    return items[:limit]


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

    async def control_presence(_request: Request) -> JSONResponse:
        # Gateway-local read of the presence monitor (room occupancy +
        # heartbeat gate). No device round-trip; returns {"enabled": False}
        # when presence monitoring is not opted in.
        monitor = getattr(gateway, "_presence", None)
        if monitor is None:
            return JSONResponse({"ok": True, "enabled": False})
        return JSONResponse({"ok": True, **monitor.snapshot()})

    async def control_presence_config(request: Request) -> JSONResponse:
        # Runtime-adjust the absent debounce / sleeping-hours window. The
        # monitor persists them so a dashboard change survives a restart.
        body = await _read_json_body(request)
        monitor = getattr(gateway, "_presence", None)
        if monitor is None:
            return _control_error("presence monitor not running", status=503)
        absent_after_s = body.get("absent_after_s")
        sleep_window = body.get("sleep_window")
        if absent_after_s is None and sleep_window is None:
            return _control_error(
                "provide absent_after_s and/or sleep_window", status=400
            )
        if absent_after_s is not None and (
            not isinstance(absent_after_s, int) or isinstance(absent_after_s, bool)
        ):
            return _control_error("absent_after_s must be an integer", status=400)
        if sleep_window is not None and not isinstance(sleep_window, str):
            return _control_error("sleep_window must be a string", status=400)
        result = monitor.update_config(
            absent_after_s=absent_after_s, sleep_window=sleep_window
        )
        return _control_json(result, status=200 if result.get("ok") else 400)

    async def control_presence_report(request: Request) -> JSONResponse:
        # Gateway-local: aggregate the presence log into a self-diagnostic
        # report (occupancy separation, in-room valleys, a recommended
        # absent_after_s). Read-only — it never applies the recommendation
        # (human-in-the-loop; closing the loop is Phase 3).
        monitor = getattr(gateway, "_presence", None)
        if monitor is None:
            return JSONResponse({"ok": True, "enabled": False})
        days = _parse_days(request.query_params.get("days"))
        # The log read + aggregation can touch many rows; keep it off the
        # event loop so other control requests / the WebSocket never stall.
        report = await asyncio.to_thread(monitor.build_report, days=days)
        return JSONResponse({"ok": True, **report})

    async def control_activity(request: Request) -> JSONResponse:
        # Gateway-local activity feed (yorishiro): merge the autonomous
        # activity JSONL (heartbeat / proactive / presence / home) with the
        # daily presence reports into one reverse-chronological list for the
        # dashboard. Read-only; the disk read + merge runs off the event loop
        # (cf. control_presence_report).
        limit = _parse_limit(request.query_params.get("limit"))
        source = request.query_params.get("source") or None
        items = await asyncio.to_thread(_gather_activity, limit, source)
        return JSONResponse({"ok": True, "items": items})

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

    async def control_routing(request: Request) -> JSONResponse:
        # Gateway-only state (no device round-trip): pin every voice turn
        # to Hermes when force_hermes is true, else auto-route.
        body = await _read_json_body(request)
        force = body.get("force_hermes")
        if not isinstance(force, bool):
            return _control_error("force_hermes must be a boolean", status=400)
        return _control_json(control.set_routing_force_hermes(force))

    async def control_multiturn(request: Request) -> JSONResponse:
        # Gateway-only state (no device round-trip): when enabled, a Hermes
        # reply ending in a question re-opens listening for a hands-free
        # follow-up (bounded; see stackchan_mcp.multiturn). The persisted
        # toggle is the runtime source of truth — it overrides the legacy
        # STACKCHAN_MULTITURN env gate.
        body = await _read_json_body(request)
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            return _control_error("enabled must be a boolean", status=400)
        return _control_json(control.set_multiturn(enabled))

    async def control_proactive(request: Request) -> JSONResponse:
        # Gateway-only state (no device round-trip): the runtime kill switch
        # for state-transition-driven proactive speech. The persisted toggle
        # is read live by ProactiveSpeaker.on_state_change, so this takes
        # effect on the next transition without a restart. Note it only
        # matters when the speaker exists (STACKCHAN_PROACTIVE env set).
        body = await _read_json_body(request)
        enabled = body.get("proactive_enabled")
        if not isinstance(enabled, bool):
            return _control_error("proactive_enabled must be a boolean", status=400)
        return _control_json(control.set_proactive_enabled(enabled))

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

    async def control_i2c(request: Request) -> JSONResponse:
        # Debug relay onto the Grove Port A I2C bus (yorishiro sensor
        # bring-up, "道A"): probe new Port A sensors without a firmware
        # change. Mirrors the MCP i2c_* tools over the control plane.
        # Body: {"op": "scan"|"read"|"write"|"write_read", ...args}.
        body = await _read_json_body(request)
        if not gateway.esp32.device_connected:
            return _control_error("no device connected", status=503)
        op = body.get("op")
        if op == "scan":
            content = await _dispatch_mcp_tool("i2c_scan", {}, gateway)
            return _control_i2c_result(content)
        if op not in ("read", "write", "write_read"):
            return _control_error(
                "op must be one of: scan, read, write, write_read", status=400
            )
        addr = body.get("addr")
        if (
            not isinstance(addr, int)
            or isinstance(addr, bool)
            or not 0x08 <= addr <= 0x77
        ):
            return _control_error("addr must be an integer 0x08..0x77", status=400)
        if op == "read":
            n = _i2c_n_bytes(body.get("n_bytes"))
            if n is None:
                return _control_error("n_bytes must be an integer 1..256", status=400)
            content = await _dispatch_mcp_tool(
                "i2c_read", {"addr": addr, "n_bytes": n}, gateway
            )
        elif op == "write":
            data = _i2c_byte_list(body.get("bytes"))
            if data is None:
                return _control_error(
                    "bytes must be a non-empty list of integers 0..255", status=400
                )
            content = await _dispatch_mcp_tool(
                "i2c_write", {"addr": addr, "bytes": data}, gateway
            )
        else:  # write_read
            data = _i2c_byte_list(body.get("write_bytes"))
            n = _i2c_n_bytes(body.get("n_bytes"))
            if data is None:
                return _control_error(
                    "write_bytes must be a non-empty list of integers 0..255",
                    status=400,
                )
            if n is None:
                return _control_error("n_bytes must be an integer 1..256", status=400)
            content = await _dispatch_mcp_tool(
                "i2c_write_read",
                {"addr": addr, "write_bytes": data, "n_bytes": n},
                gateway,
            )
        return _control_i2c_result(content)

    async def control_sensors(_request: Request) -> JSONResponse:
        # Live Port A sensor snapshot for the dashboard sensor tab
        # (yorishiro): TMOS PIR presence/motion/temp + PAJ7620 gesture.
        # Per-sensor errors are nested in the payload (one sensor NACKing
        # must not 502 the whole read), so top-level ok stays True.
        if not gateway.esp32.device_connected:
            return _control_error("no device connected", status=503)

        async def dispatch(name: str, arguments: dict[str, Any]) -> list[Any]:
            return await _dispatch_mcp_tool(name, arguments, gateway)

        data = await sensors.read_all(dispatch)
        return _control_json({"ok": True, **data})

    async def control_sensors_init(_request: Request) -> JSONResponse:
        # Enable the TMOS embedded algorithm (ODR/BDU) and write the
        # PAJ7620 gesture-mode init array. The dashboard calls this when
        # the sensor poll toggle is switched on. Per-sensor ok is nested.
        if not gateway.esp32.device_connected:
            return _control_error("no device connected", status=503)

        async def dispatch(name: str, arguments: dict[str, Any]) -> list[Any]:
            return await _dispatch_mcp_tool(name, arguments, gateway)

        data = await sensors.init_all(dispatch)
        return _control_json({"ok": True, **data})

    # ---- Mode presets (yorishiro fork) --------------------------------
    async def control_presets_list(_request: Request) -> JSONResponse:
        return _control_json({"ok": True, "presets": await control.list_presets()})

    async def control_presets_save(request: Request) -> JSONResponse:
        body = await _read_json_body(request)
        if not gateway.esp32.device_connected:
            # Need the device to snapshot proximity, so require connection.
            return _control_error("no device connected", status=503)
        name = control.normalize_preset_name(body.get("name"))
        if name is None:
            return _control_error(
                "name must be 1..32 chars without / \\ or ..", status=400
            )
        snapshot = await _build_preset_snapshot(gateway)
        result = await control.save_preset(
            name, snapshot, overwrite=bool(body.get("overwrite", False))
        )
        if result.get("ok"):
            return _control_json(result)
        status = 409 if "already exists" in result.get("error", "") else 502
        return _control_json(result, status=status)

    async def control_presets_apply(request: Request) -> JSONResponse:
        body = await _read_json_body(request)
        if not gateway.esp32.device_connected:
            return _control_error("no device connected", status=503)
        name = control.normalize_preset_name(body.get("name"))
        if name is None:
            return _control_error("invalid preset name", status=400)
        result = await control.apply_preset(gateway, name)
        if result.get("ok"):
            return _control_json(result)
        err = result.get("error", "")
        status = 404 if "not found" in err else 409 if "busy" in err else 502
        return _control_json(result, status=status)

    async def control_presets_delete(request: Request) -> JSONResponse:
        body = await _read_json_body(request)
        name = control.normalize_preset_name(body.get("name"))
        if name is None:
            return _control_error("invalid preset name", status=400)
        result = await control.delete_preset(name)
        if result.get("ok"):
            return _control_json(result)
        return _control_json(result, status=404)

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
        Route("/control/presence", endpoint=control_presence, methods=["GET"]),
        Route(
            "/control/presence/report",
            endpoint=control_presence_report,
            methods=["GET"],
        ),
        Route(
            "/control/presence/config",
            endpoint=control_presence_config,
            methods=["POST"],
        ),
        Route("/control/activity", endpoint=control_activity, methods=["GET"]),
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
        Route("/control/routing", endpoint=control_routing, methods=["POST"]),
        Route("/control/multiturn", endpoint=control_multiturn, methods=["POST"]),
        Route("/control/proactive", endpoint=control_proactive, methods=["POST"]),
        Route("/control/avatar", endpoint=control_avatar, methods=["POST"]),
        Route("/control/say", endpoint=control_say, methods=["POST"]),
        Route("/control/i2c", endpoint=control_i2c, methods=["POST"]),
        Route("/control/sensors", endpoint=control_sensors, methods=["GET"]),
        Route(
            "/control/sensors/init",
            endpoint=control_sensors_init,
            methods=["POST"],
        ),
        Route(
            "/control/presets/list",
            endpoint=control_presets_list,
            methods=["GET"],
        ),
        Route(
            "/control/presets/save",
            endpoint=control_presets_save,
            methods=["POST"],
        ),
        Route(
            "/control/presets/apply",
            endpoint=control_presets_apply,
            methods=["POST"],
        ),
        Route(
            "/control/presets/delete",
            endpoint=control_presets_delete,
            methods=["POST"],
        ),
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
