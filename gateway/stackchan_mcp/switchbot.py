"""SwitchBot Cloud API v1.1 client for home-appliance control (Phase C).

yorishiro fork specific module (not intended for upstream PR).

Hermes drives the gateway over the Streamable HTTP MCP server, so
exposing SwitchBot devices as gateway tools gives the agent home
control without a new service, port or Hermes-side registration. The
client speaks the official v1.1 REST API directly:

    https://github.com/OpenWonderLabs/SwitchBotAPI

Authentication follows the v1.1 scheme: every request carries the
open token plus an HMAC-SHA256 signature of ``token + t + nonce``
(``t`` = 13-digit millisecond timestamp, ``nonce`` = random UUID),
base64-encoded. The implementation mirrors the official Python 3
example from the README above.

Environment variables:

- ``SWITCHBOT_TOKEN`` — open token issued by the SwitchBot app
  (Profile → Preferences → Developer Options). Required.
- ``SWITCHBOT_SECRET`` — secret key issued alongside the token.
  Required.
- ``SWITCHBOT_API_URL`` — API base URL override (tests / proxies).
  Defaults to ``https://api.switch-bot.com/v1.1``.

Without token + secret the tool calls fail with a clear RuntimeError;
the gateway itself (startup, tool listing) is unaffected.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from typing import Any
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.switch-bot.com/v1.1"

#: One cloud round-trip; past this the voice interaction has stalled
#: anyway and the caller should surface the error.
API_TIMEOUT_S = 10.0

#: SwitchBot signals success via ``statusCode`` in the response body
#: (HTTP 200 does not imply success).
SUCCESS_STATUS_CODE = 100

#: MCP tool names backed by this module. Single-sourced here so the
#: HTTP daemon's BYPASS_TOOLS (these tools never touch the ESP32, so
#: they must not enter the single-flight device queue) stays in sync
#: with the dispatch branches in :mod:`stackchan_mcp.stdio_server`.
TOOL_NAMES = frozenset(
    {
        "switchbot_list_devices",
        "switchbot_get_status",
        "switchbot_send_command",
    }
)


def is_configured() -> bool:
    """True when both SWITCHBOT_TOKEN and SWITCHBOT_SECRET are set."""
    return bool(
        os.getenv("SWITCHBOT_TOKEN", "").strip()
        and os.getenv("SWITCHBOT_SECRET", "").strip()
    )


def build_auth_headers(
    token: str,
    secret: str,
    *,
    t: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Build the v1.1 auth headers: Authorization / sign / t / nonce.

    ``sign`` = base64(HMAC-SHA256(secret, token + t + nonce)), exactly
    as in the official Python 3 example (the README prose mentions
    uppercasing, but the reference example — and the widely deployed
    clients that follow it — send the base64 digest as-is).
    ``t`` and ``nonce`` are injectable for deterministic tests.
    """
    if t is None:
        t = int(round(time.time() * 1000))
    if nonce is None:
        nonce = str(uuid.uuid4())
    string_to_sign = f"{token}{t}{nonce}".encode("utf-8")
    sign = base64.b64encode(
        hmac.new(
            secret.encode("utf-8"),
            msg=string_to_sign,
            digestmod=hashlib.sha256,
        ).digest()
    ).decode("utf-8")
    return {
        "Authorization": token,
        "sign": sign,
        "t": str(t),
        "nonce": nonce,
        "Content-Type": "application/json; charset=utf8",
    }


async def _request(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Perform one authenticated v1.1 request, return the response body.

    Raises RuntimeError on any non-usable response (missing
    credentials, transport failure, non-200 HTTP status, or a body
    ``statusCode`` other than :data:`SUCCESS_STATUS_CODE`) so callers
    get one uniform error discipline.
    """
    token = os.getenv("SWITCHBOT_TOKEN", "").strip()
    secret = os.getenv("SWITCHBOT_SECRET", "").strip()
    if not token or not secret:
        raise RuntimeError(
            "SwitchBot API is not configured — set SWITCHBOT_TOKEN and "
            "SWITCHBOT_SECRET (SwitchBot app: Profile → Preferences → "
            "Developer Options)"
        )
    base_url = os.getenv("SWITCHBOT_API_URL", DEFAULT_API_URL).rstrip("/")
    headers = build_auth_headers(token, secret)

    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_S)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                method, f"{base_url}{path}", json=json_body, headers=headers
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    logger.error(
                        "SwitchBot API status=%d body=%s", resp.status, body[:500]
                    )
                    raise RuntimeError(
                        f"SwitchBot API returned status={resp.status}"
                    )
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.error("SwitchBot API request failed: %s", exc)
        raise RuntimeError(f"SwitchBot API request failed: {exc}") from exc

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        logger.error("SwitchBot API returned non-JSON body: %s", body[:500])
        raise RuntimeError("SwitchBot API returned a non-JSON response") from exc
    status_code = data.get("statusCode")
    if status_code != SUCCESS_STATUS_CODE:
        logger.error(
            "SwitchBot API statusCode=%s body=%s", status_code, body[:500]
        )
        raise RuntimeError(
            f"SwitchBot API error: statusCode={status_code} "
            f"message={data.get('message', '')}"
        )
    result = data.get("body")
    return result if isinstance(result, dict) else {}


def _require_device_id(device_id: Any) -> str:
    if not isinstance(device_id, str) or not device_id.strip():
        raise ValueError("device_id is required")
    return quote(device_id.strip(), safe="")


async def list_devices() -> dict[str, Any]:
    """GET /devices — physical ``deviceList`` + IR ``infraredRemoteList``."""
    return await _request("GET", "/devices")


async def get_device_status(device_id: str) -> dict[str, Any]:
    """GET /devices/{deviceId}/status — physical devices only."""
    return await _request("GET", f"/devices/{_require_device_id(device_id)}/status")


async def send_command(
    device_id: str,
    command: str,
    parameter: Any = "default",
    command_type: str = "command",
) -> dict[str, Any]:
    """POST /devices/{deviceId}/commands — control one device."""
    quoted_id = _require_device_id(device_id)
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command is required")
    payload = {
        "command": command.strip(),
        "parameter": parameter,
        "commandType": command_type or "command",
    }
    return await _request(
        "POST", f"/devices/{quoted_id}/commands", json_body=payload
    )
