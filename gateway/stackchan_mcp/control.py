"""Device control logic for the Phase F dashboard.

yorishiro fork specific module (not intended for upstream PR).

This module backs the gateway's ``/control/*`` REST routes
(:mod:`stackchan_mcp.http_server`). It owns the small amount of
persistent state the dashboard needs (the speaker volume and a mute
flag) and the device-facing helpers that translate dashboard actions
into ESP32 tool calls.

Design notes:

- **Volume persists across restarts.** The firmware does not remember
  the volume the user chose, so the gateway keeps it in
  ``~/.stackchan/control_state.json`` (atomic write, same flavour as
  the heartbeat state file) and re-applies it whenever a device
  (re)connects via :func:`apply_persisted_volume`.
- **Mute is gateway-side.** ``mute`` stashes the current volume and
  sets the device to 0; ``unmute`` restores it. Setting a non-zero
  volume implicitly clears the mute flag.
- **Status text never breaks a voice turn.** ``set_device_status_text``
  swallows every error (no device, old firmware without the tool, a
  transient call failure) down to a WARN log. The voice pipeline calls
  it for UI feedback only — it must never raise into the turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .gateway import Gateway

logger = logging.getLogger(__name__)

#: Where the persisted control state lives. Mirrors the heartbeat's
#: ``DEFAULT_STATE_PATH`` flavour (``~/.stackchan/...``), overridable for
#: tests / non-default deployments via ``STACKCHAN_CONTROL_STATE``.
DEFAULT_STATE_PATH = "~/.stackchan/control_state.json"

#: Default volume applied when no state file exists yet.
DEFAULT_VOLUME = 50

#: Default mic gain applied when no state file exists yet (0..36).
DEFAULT_MIC_GAIN = 30

#: Upper bound on the mic gain (matches the firmware's set_mic_gain
#: range). The dashboard / REST layer clamps to ``0..MAX_MIC_GAIN``.
MAX_MIC_GAIN = 36

#: Rolling, gateway-local record of recent conversation turns surfaced
#: by GET /control/conversation. Volatile (cleared on restart) and kept
#: out of the persisted control state, mirroring audio_stream's
#: ``_last_level`` flavour. Oldest-first; the ring drops the oldest turn
#: once it reaches ``maxlen``.
_CONVERSATION: deque[dict[str, Any]] = deque(maxlen=30)

#: Status-text strings shown on the device during a voice turn. Kept
#: here so the hermes bridge and the web-search hook share one source.
STATUS_LISTENING = "きいてるよ"
STATUS_THINKING = "考え中"
STATUS_SEARCHING = "調べ中"
STATUS_CLEAR = ""

#: Firmware tool that renders a one-line status string under the
#: avatar. Implemented on the firmware side in parallel; an old
#: firmware without it simply errors and we degrade silently.
_STATUS_TEXT_TOOL = "self.display.set_status_text"
_SET_VOLUME_TOOL = "self.audio_speaker.set_volume"
_SET_MIC_GAIN_TOOL = "self.audio_speaker.set_mic_gain"
#: Phase F dashboard extras. All three are best-effort: an old
#: firmware without the tool just errors and we degrade silently
#: (see :func:`_call_display_or_led`).
_SUBTITLE_TOOL = "self.display.set_subtitle"
_ROUTE_BADGE_TOOL = "self.display.set_route_badge"
_LED_INDICATOR_TOOL = "self.led.set_indicator"

#: How long to wait before re-applying the persisted volume on connect,
#: and how many times to retry. The codec init can swallow a set_volume
#: issued too early, so we give it a beat and one retry.
_APPLY_VOLUME_DELAY_S = 1.5
_APPLY_VOLUME_RETRIES = 1


def _state_path() -> Path:
    return Path(
        os.getenv("STACKCHAN_CONTROL_STATE", "") or DEFAULT_STATE_PATH
    ).expanduser()


def _clamp_volume(volume: Any) -> int:
    try:
        value = int(volume)
    except (TypeError, ValueError):
        return DEFAULT_VOLUME
    return min(max(value, 0), 100)


def _clamp_mic_gain(gain: Any) -> int:
    try:
        value = int(gain)
    except (TypeError, ValueError):
        return DEFAULT_MIC_GAIN
    return min(max(value, 0), MAX_MIC_GAIN)


def load_state() -> dict[str, Any]:
    """Read the persisted control state, with defaults filled in.

    Returns a dict with ``volume`` (int 0..100), ``muted`` (bool),
    ``pre_mute_volume`` (int 0..100) and ``mic_gain`` (int 0..36). A
    missing or unreadable file yields the defaults rather than raising —
    the dashboard must come up even on a fresh host.
    """
    path = _state_path()
    raw: dict[str, Any] = {}
    try:
        data = json.loads(path.read_text("utf-8"))
        if isinstance(data, dict):
            raw = data
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        logger.warning("control: unreadable state file %s (%s)", path, exc)
    volume = _clamp_volume(raw.get("volume", DEFAULT_VOLUME))
    pre_mute = _clamp_volume(raw.get("pre_mute_volume", volume))
    muted = bool(raw.get("muted", False))
    mic_gain = _clamp_mic_gain(raw.get("mic_gain", DEFAULT_MIC_GAIN))
    return {
        "volume": volume,
        "muted": muted,
        "pre_mute_volume": pre_mute,
        "mic_gain": mic_gain,
    }


def save_state(state: dict[str, Any]) -> None:
    """Persist the control state atomically (write-temp + os.replace)."""
    path = _state_path()
    payload = {
        "volume": _clamp_volume(state.get("volume", DEFAULT_VOLUME)),
        "muted": bool(state.get("muted", False)),
        "pre_mute_volume": _clamp_volume(
            state.get("pre_mute_volume", state.get("volume", DEFAULT_VOLUME))
        ),
        "mic_gain": _clamp_mic_gain(state.get("mic_gain", DEFAULT_MIC_GAIN)),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except OSError as exc:
        logger.warning("control: cannot write state file %s (%s)", path, exc)


async def _send_volume(gateway: "Gateway", volume: int) -> bool:
    """Push a volume level to the device. True on success."""
    result, error = await gateway.esp32.call_tool(
        _SET_VOLUME_TOOL, {"volume": volume}
    )
    if error:
        logger.warning("control: set_volume failed: %s", error)
        return False
    return True


async def set_volume(gateway: "Gateway", volume: Any) -> dict[str, Any]:
    """Set the speaker volume (0..100), persist it, and clear mute.

    Returns ``{"ok": True, "volume": int, "muted": False}`` on success
    or ``{"ok": False, "error": ...}`` when the device call fails.
    """
    target = _clamp_volume(volume)
    state = load_state()
    if not await _send_volume(gateway, target):
        return {"ok": False, "error": "device call failed"}
    state["volume"] = target
    state["muted"] = False
    state["pre_mute_volume"] = target
    save_state(state)
    return {"ok": True, "volume": target, "muted": False}


async def mute(gateway: "Gateway") -> dict[str, Any]:
    """Mute the speaker, stashing the current volume for restore."""
    state = load_state()
    if not state["muted"]:
        state["pre_mute_volume"] = state["volume"]
    if not await _send_volume(gateway, 0):
        return {"ok": False, "error": "device call failed"}
    state["volume"] = 0
    state["muted"] = True
    save_state(state)
    return {"ok": True, "volume": 0, "muted": True}


async def unmute(gateway: "Gateway") -> dict[str, Any]:
    """Restore the volume stashed by :func:`mute`."""
    state = load_state()
    restore = _clamp_volume(state.get("pre_mute_volume", DEFAULT_VOLUME))
    if not await _send_volume(gateway, restore):
        return {"ok": False, "error": "device call failed"}
    state["volume"] = restore
    state["muted"] = False
    save_state(state)
    return {"ok": True, "volume": restore, "muted": False}


async def apply_persisted_volume(gateway: "Gateway") -> None:
    """Re-apply the saved volume after a device (re)connects.

    The firmware does not persist the user's chosen volume, so the
    gateway restores it on connect. The codec init can drop a
    set_volume issued the instant the device appears, so this waits a
    beat and retries once. A muted state restores to 0. Errors are
    swallowed to WARN — a failed restore must not take anything down.
    """
    state = load_state()
    target = 0 if state["muted"] else state["volume"]
    await asyncio.sleep(_APPLY_VOLUME_DELAY_S)
    for attempt in range(_APPLY_VOLUME_RETRIES + 1):
        if not gateway.esp32.device_connected:
            logger.info("control: device gone before volume re-apply")
            return
        try:
            if await _send_volume(gateway, target):
                logger.info("control: re-applied volume=%d (muted=%s)", target, state["muted"])
                return
        except Exception:
            logger.exception("control: volume re-apply raised")
        if attempt < _APPLY_VOLUME_RETRIES:
            await asyncio.sleep(_APPLY_VOLUME_DELAY_S)
    logger.warning("control: volume re-apply gave up after retries")


async def _send_mic_gain(gateway: "Gateway", gain: int) -> bool:
    """Push a mic gain level to the device. True on success."""
    result, error = await gateway.esp32.call_tool(
        _SET_MIC_GAIN_TOOL, {"gain": gain}
    )
    if error:
        logger.warning("control: set_mic_gain failed: %s", error)
        return False
    return True


async def set_mic_gain(gateway: "Gateway", gain: Any) -> dict[str, Any]:
    """Set the mic gain (0..36), persist it.

    Returns ``{"ok": True, "gain": int}`` on success or
    ``{"ok": False, "error": ...}`` when the device call fails.
    """
    target = _clamp_mic_gain(gain)
    state = load_state()
    if not await _send_mic_gain(gateway, target):
        return {"ok": False, "error": "device call failed"}
    state["mic_gain"] = target
    save_state(state)
    return {"ok": True, "gain": target}


async def apply_persisted_mic_gain(gateway: "Gateway") -> None:
    """Re-apply the saved mic gain after a device (re)connects.

    The firmware does not persist the user's chosen mic gain, so the
    gateway restores it on connect. The codec init can drop a
    set_mic_gain issued the instant the device appears, so this waits a
    beat and retries once. Errors are swallowed to WARN — a failed
    restore must not take anything down. Mirrors
    :func:`apply_persisted_volume`.
    """
    state = load_state()
    target = state["mic_gain"]
    await asyncio.sleep(_APPLY_VOLUME_DELAY_S)
    for attempt in range(_APPLY_VOLUME_RETRIES + 1):
        if not gateway.esp32.device_connected:
            logger.info("control: device gone before mic_gain re-apply")
            return
        try:
            if await _send_mic_gain(gateway, target):
                logger.info("control: re-applied mic_gain=%d", target)
                return
        except Exception:
            logger.exception("control: mic_gain re-apply raised")
        if attempt < _APPLY_VOLUME_RETRIES:
            await asyncio.sleep(_APPLY_VOLUME_DELAY_S)
    logger.warning("control: mic_gain re-apply gave up after retries")


async def set_device_status_text(gateway: "Gateway", text: str) -> None:
    """Show a one-line status string on the device (empty = clear).

    Never raises: a missing device, an old firmware without the
    ``set_status_text`` tool, or a transient failure are all logged at
    WARN and otherwise ignored. This is called from the voice turn for
    UI feedback only and must never break the conversation.
    """
    if not gateway.esp32.device_connected:
        return
    try:
        _result, error = await gateway.esp32.call_tool(
            _STATUS_TEXT_TOOL, {"text": text}
        )
        if error:
            logger.warning("control: set_status_text failed: %s", error)
    except Exception:
        logger.warning("control: set_status_text raised", exc_info=True)


async def _best_effort_device_call(
    gateway: "Gateway", tool: str, args: dict[str, Any], label: str
) -> None:
    """Call a display/LED device tool, swallowing every failure to WARN.

    Phase F dashboard cosmetics (subtitle, route badge, LED indicator)
    must never break a voice turn: a missing device, an old firmware
    without the tool, or a transient failure are all logged at WARN
    and otherwise ignored. Mirrors :func:`set_device_status_text`.
    """
    if not gateway.esp32.device_connected:
        return
    try:
        _result, error = await gateway.esp32.call_tool(tool, args)
        if error:
            logger.warning("control: %s failed: %s", label, error)
    except Exception:
        logger.warning("control: %s raised", label, exc_info=True)


async def set_device_subtitle(gateway: "Gateway", text: str) -> None:
    """Show the spoken reply as a subtitle on the device (empty = clear).

    Best-effort cosmetic; see :func:`_best_effort_device_call`.
    """
    await _best_effort_device_call(
        gateway, _SUBTITLE_TOOL, {"text": text}, "set_subtitle"
    )


async def set_device_route_badge(gateway: "Gateway", text: str) -> None:
    """Set the LLM-route badge ("H" for Hermes, "" to clear).

    Best-effort cosmetic; see :func:`_best_effort_device_call`.
    """
    await _best_effort_device_call(
        gateway, _ROUTE_BADGE_TOOL, {"text": text}, "set_route_badge"
    )


async def set_device_led_indicator(
    gateway: "Gateway", r: int, g: int, b: int
) -> None:
    """Set the indicator LED colour (0,0,0 turns it off).

    Best-effort cosmetic; see :func:`_best_effort_device_call`. The
    gateway only drives this LED during the response phase — listening
    (green) stays firmware-autonomous, so callers must clear it (0,0,0)
    in a finally to avoid stomping the firmware's own LED state.
    """
    await _best_effort_device_call(
        gateway, _LED_INDICATOR_TOOL, {"r": r, "g": g, "b": b}, "set_indicator"
    )


def record_conversation_turn(
    transcript: str,
    reply: str,
    route: str,
    timings_ms: dict[str, Any] | None = None,
) -> None:
    """Append one completed voice turn to the rolling conversation log.

    Called from the hermes bridge once a turn has produced both a
    transcript and a spoken reply (TTS sent). The ring buffer is
    volatile (gateway-local, cleared on restart) and is not mixed into
    the persisted control state. ``ts`` is a wall-clock epoch float.
    """
    _CONVERSATION.append(
        {
            "ts": time.time(),
            "transcript": transcript,
            "reply": reply,
            "route": route,
            "timings_ms": timings_ms,
        }
    )


def get_conversation() -> dict[str, Any]:
    """Return the rolling conversation log for GET /control/conversation.

    ``turns`` is oldest-first (append order; newest last). Reads the
    gateway-local ring buffer; no device round-trip.
    """
    return {"ok": True, "turns": list(_CONVERSATION)}


def get_audio_level() -> dict[str, Any]:
    """Return the live mic input level for GET /control/audio_level.

    ``recording`` reflects whether a capture slot is open; ``level`` is
    the most recent frame's RMS normalised to 0.0-1.0 (0.0 when not
    recording). Reads straight off :mod:`stackchan_mcp.audio_stream`.
    """
    from . import audio_stream

    recording = audio_stream.is_recording()
    level = audio_stream.get_input_level() if recording else 0.0
    return {"ok": True, "recording": recording, "level": level}


async def trigger_listen(gateway: "Gateway") -> dict[str, Any]:
    """Fire a device-driven listen (tap-equivalent) from the dashboard.

    Returns ``{"ok": False, "error": "already listening"}`` when a
    recording slot is already open (an MCP- or device-driven listen is
    in flight), mirroring the firmware-side guard. Otherwise sends a
    ``listen.start`` so the device records exactly as it would on an
    LCD tap; the existing audio-hook pipeline forwards the capture to
    ``/voice_turn``.
    """
    if not gateway.esp32.device_connected:
        return {"ok": False, "error": "no device connected"}
    from .audio_stream import is_recording

    if is_recording():
        return {"ok": False, "error": "already listening"}
    try:
        await gateway.esp32.send_listen_state("start", mode="manual")
    except Exception as exc:
        logger.warning("control: trigger_listen failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {"ok": True}
