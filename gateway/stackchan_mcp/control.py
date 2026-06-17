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

#: Where named mode presets live (one JSON file per preset). Overridable
#: for tests via ``STACKCHAN_PRESETS_DIR``. Each preset is a snapshot of
#: the dashboard-controllable settings (volume/mic/brightness/LED/
#: proximity/heartbeat) that :func:`apply_preset` re-sends in one shot.
DEFAULT_PRESETS_PATH = "~/.stackchan/presets"

#: Upper bound on a preset name's length (the name doubles as filename).
MAX_PRESET_NAME_LEN = 32

#: Default volume applied when no state file exists yet.
DEFAULT_VOLUME = 50

#: Default response routing. When True, every voice turn is pinned to
#: Hermes and the local LLM fast-path is bypassed. Dashboard-toggleable
#: and persisted in the control state; defaults off (auto-routing, the
#: pre-toggle behaviour).
DEFAULT_FORCE_HERMES = False

#: Default mic gain applied when no state file exists yet (0..36).
DEFAULT_MIC_GAIN = 30

#: Upper bound on the mic gain (matches the firmware's set_mic_gain
#: range). The dashboard / REST layer clamps to ``0..MAX_MIC_GAIN``.
MAX_MIC_GAIN = 36

#: Default screen brightness (0..100). Matches the firmware's own NVS
#: default so a fresh gateway and a fresh device agree. Unlike volume,
#: the firmware *does* persist brightness to NVS (set_brightness saves),
#: so the gateway copy is mainly for the dashboard to show a value and
#: to re-assert the user's choice on reconnect.
DEFAULT_BRIGHTNESS = 75

#: Default LED state, one colour per phase ("slot"). r/g/b are each
#: 0..255 (WS2812, 12 LEDs). Only ``idle`` has an on/off — it is off by
#: default (the device is dark between conversations); ``listening`` and
#: ``hermes`` are always shown during their phase:
#:   - ``idle``      … not in a conversation (user colour, default off)
#:   - ``listening`` … recording + local-LLM thinking/preparing
#:   - ``hermes``    … Hermes agent is running
DEFAULT_LED: dict[str, Any] = {
    "brightness": 100,
    "idle": {"on": False, "r": 30, "g": 144, "b": 255},
    "listening": {"r": 0, "g": 210, "b": 90},
    "hermes": {"r": 148, "g": 108, "b": 255},
}

#: The configurable LED slots, in dashboard display order.
LED_SLOTS = ("idle", "listening", "hermes")

#: How long :func:`preview_led` lights a slot's colour before reverting
#: to the idle state (so the dashboard can show a colour you would
#: otherwise only see for ~1 s mid-conversation).
LED_PREVIEW_SECONDS = 1.5

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
#: Screen brightness (0..100) and base LED tools. ``set_all`` lights all
#: 12 LEDs one colour and (unlike ``set_indicator``) does not re-arm the
#: firmware's 60 s idle-settle timer; ``clear`` turns them all off.
_SET_BRIGHTNESS_TOOL = "self.screen.set_brightness"
_SET_ALL_LEDS_TOOL = "self.led.set_all"
_CLEAR_LEDS_TOOL = "self.led.clear"
#: Phase F dashboard joystick. ``set_head_angles`` is a live move (not
#: persisted); ``set_neutral_pose`` writes the rest pose the head
#: returns to and persists it to NVS on the firmware side. Both clamp
#: yaw to ``[-90,90]`` and pitch to ``[5,85]`` (the M5Stack-recommended
#: range); the firmware applies its own wider hard clamp (pitch 0..88)
#: on top.
_SET_HEAD_ANGLES_TOOL = "self.robot.set_head_angles"
_SET_NEUTRAL_POSE_TOOL = "self.robot.set_neutral_pose"

#: Head-angle bounds the dashboard / REST layer clamps to. Yaw is the
#: horizontal pan; pitch is the vertical tilt. See the tool constants
#: above for why pitch floors at 5 rather than 0.
MIN_HEAD_YAW = -90
MAX_HEAD_YAW = 90
MIN_HEAD_PITCH = 5
MAX_HEAD_PITCH = 85
#: Phase F dashboard extras. All three are best-effort: an old
#: firmware without the tool just errors and we degrade silently
#: (see :func:`_call_display_or_led`).
_SUBTITLE_TOOL = "self.display.set_subtitle"
_ROUTE_BADGE_TOOL = "self.display.set_route_badge"
_LED_INDICATOR_TOOL = "self.led.set_indicator"
#: Touch/proximity config tool (yorishiro fork). Full device name so the
#: control layer can use ``call_tool`` directly (mirrors the short-name →
#: full-name mapping in stdio_server's dispatch table).
_SET_PROXIMITY_TOOL = "self.touch.set_proximity_config"

#: How long to wait before re-applying the persisted volume on connect,
#: and how many times to retry. The codec init can swallow a set_volume
#: issued too early, so we give it a beat and one retry.
_APPLY_VOLUME_DELAY_S = 1.5
_APPLY_VOLUME_RETRIES = 1

#: Serialises mute/unmute. Both do a read-modify-write on the state file
#: (load_state → _send_volume → save_state); two dashboard taps racing
#: could otherwise stash 0 into pre_mute_volume and lose the real level.
_mute_lock = asyncio.Lock()

#: Serialises preset application. ``apply_preset`` re-sends a whole batch
#: of settings; two concurrent applies (or an apply racing a voice turn)
#: would interleave device calls and corrupt the LED / volume state.
_preset_lock = asyncio.Lock()


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


def _clamp_brightness(value: Any) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return DEFAULT_BRIGHTNESS
    return min(max(v, 0), 100)


def _clamp_rgb(value: Any) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return 0
    return min(max(v, 0), 255)


def _clamp_color(raw: Any, default: dict[str, Any]) -> dict[str, int]:
    src = raw if isinstance(raw, dict) else {}
    return {
        "r": _clamp_rgb(src.get("r", default["r"])),
        "g": _clamp_rgb(src.get("g", default["g"])),
        "b": _clamp_rgb(src.get("b", default["b"])),
    }


def _clamp_led_brightness(value: Any) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return DEFAULT_LED["brightness"]
    return min(max(v, 0), 100)


def _scale_rgb(r: int, g: int, b: int, brightness: int) -> dict[str, int]:
    """Scale an r/g/b triple by a 0..100 brightness percentage."""
    return {
        "r": r * brightness // 100,
        "g": g * brightness // 100,
        "b": b * brightness // 100,
    }


def _normalize_led(raw: Any) -> dict[str, Any]:
    """Coerce a stored/incoming LED blob to the 3-slot structure.

    Each slot is a colour ``{r, g, b}``; ``idle`` also carries ``on``.
    Backward-compatible with the old flat ``{on, r, g, b}`` shape (it
    becomes the ``idle`` slot) and fills any missing slot from
    :data:`DEFAULT_LED`.
    """
    src = raw if isinstance(raw, dict) else {}
    # Old flat shape (pre-3-slot): migrate it to the idle slot.
    if "idle" not in src and ("on" in src or "r" in src):
        src = {"idle": src}
    idle_src = src.get("idle") if isinstance(src.get("idle"), dict) else {}
    idle = {
        "on": bool(idle_src.get("on", DEFAULT_LED["idle"]["on"])),
        **_clamp_color(idle_src, DEFAULT_LED["idle"]),
    }
    return {
        "brightness": _clamp_led_brightness(
            src.get("brightness", DEFAULT_LED["brightness"])
        ),
        "idle": idle,
        "listening": _clamp_color(src.get("listening"), DEFAULT_LED["listening"]),
        "hermes": _clamp_color(src.get("hermes"), DEFAULT_LED["hermes"]),
    }


def _clamp_head_yaw(yaw: Any) -> int:
    try:
        value = int(yaw)
    except (TypeError, ValueError):
        return 0
    return min(max(value, MIN_HEAD_YAW), MAX_HEAD_YAW)


def _clamp_head_pitch(pitch: Any) -> int:
    try:
        value = int(pitch)
    except (TypeError, ValueError):
        return MIN_HEAD_PITCH
    return min(max(value, MIN_HEAD_PITCH), MAX_HEAD_PITCH)


def _default_multiturn() -> bool:
    """Initial multi-turn default when the state file has no ``multiturn`` key.

    Mirrors the env truthiness parse in :func:`multiturn.is_enabled` (kept
    local to avoid a control→multiturn import cycle). This lets the legacy
    ``STACKCHAN_MULTITURN`` env gate seed the first value on an existing
    host, after which the persisted dashboard toggle is the source of truth
    — a dashboard OFF wins even when the env var is still set.
    """
    return os.getenv("STACKCHAN_MULTITURN", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def load_state() -> dict[str, Any]:
    """Read the persisted control state, with defaults filled in.

    Returns a dict with ``volume`` (int 0..100), ``muted`` (bool),
    ``pre_mute_volume`` (int 0..100), ``mic_gain`` (int 0..36),
    ``brightness`` (int 0..100), ``led`` (``{on, r, g, b}``),
    ``force_hermes`` (bool) and ``multiturn`` (bool). A missing or
    unreadable file yields the defaults rather than raising —
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
    brightness = _clamp_brightness(raw.get("brightness", DEFAULT_BRIGHTNESS))
    led = _normalize_led(raw.get("led", DEFAULT_LED))
    force_hermes = bool(raw.get("force_hermes", DEFAULT_FORCE_HERMES))
    multiturn = bool(raw.get("multiturn", _default_multiturn()))
    return {
        "volume": volume,
        "muted": muted,
        "pre_mute_volume": pre_mute,
        "mic_gain": mic_gain,
        "brightness": brightness,
        "led": led,
        "force_hermes": force_hermes,
        "multiturn": multiturn,
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
        "brightness": _clamp_brightness(state.get("brightness", DEFAULT_BRIGHTNESS)),
        "led": _normalize_led(state.get("led", DEFAULT_LED)),
        "force_hermes": bool(state.get("force_hermes", DEFAULT_FORCE_HERMES)),
        "multiturn": bool(state.get("multiturn", _default_multiturn())),
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


def routing_force_hermes() -> bool:
    """True when voice turns are pinned to Hermes (local fast-path off).

    Reads the persisted control state so the setting survives a gateway
    restart, mirroring how volume / mic_gain / led are surfaced.
    """
    return bool(load_state()["force_hermes"])


def set_routing_force_hermes(enabled: bool) -> dict[str, Any]:
    """Persist the Hermes-pin toggle and echo the new value back."""
    state = load_state()
    state["force_hermes"] = bool(enabled)
    save_state(state)
    return {"ok": True, "force_hermes": bool(enabled)}


def multiturn_enabled() -> bool:
    """True when hands-free multi-turn continuation is on (persisted).

    Reads the persisted control state so the setting survives a gateway
    restart, mirroring :func:`routing_force_hermes`. This is the runtime
    source of truth for the multi-turn feature; the legacy
    ``STACKCHAN_MULTITURN`` env var only seeds the default (see
    :func:`_default_multiturn`), so a dashboard OFF wins over the env.
    """
    return bool(load_state()["multiturn"])


def set_multiturn(enabled: bool) -> dict[str, Any]:
    """Persist the multi-turn toggle and echo the new value back."""
    state = load_state()
    state["multiturn"] = bool(enabled)
    save_state(state)
    return {"ok": True, "multiturn": bool(enabled)}


def is_muted() -> bool:
    """True when the speaker is muted (persisted control state).

    A thin accessor mirroring :func:`routing_force_hermes` so callers
    (e.g. the multi-turn continuation check) can read just the mute flag
    without unpacking the whole state dict — and tests can stub it.
    """
    return bool(load_state()["muted"])


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
    async with _mute_lock:
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
    async with _mute_lock:
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


async def _send_brightness(gateway: "Gateway", value: int) -> bool:
    """Push a screen brightness to the device. True on success."""
    _result, error = await gateway.esp32.call_tool(
        _SET_BRIGHTNESS_TOOL, {"brightness": value}
    )
    if error:
        logger.warning("control: set_brightness failed: %s", error)
        return False
    return True


async def set_brightness(gateway: "Gateway", value: Any) -> dict[str, Any]:
    """Set the screen brightness (0..100) and persist it.

    The firmware also saves brightness to its own NVS, so this gateway
    copy is for the dashboard to display and to re-assert on reconnect.
    Returns ``{"ok": True, "brightness": int}`` on success or
    ``{"ok": False, "error": ...}`` when the device call fails.
    """
    target = _clamp_brightness(value)
    state = load_state()
    if not await _send_brightness(gateway, target):
        return {"ok": False, "error": "device call failed"}
    state["brightness"] = target
    save_state(state)
    return {"ok": True, "brightness": target}


async def apply_persisted_brightness(gateway: "Gateway") -> None:
    """Re-assert the saved brightness after a device (re)connects.

    Harmless even though the firmware restores its own NVS value on
    boot: the two are kept in sync (every set_brightness saves NVS), so
    this just confirms the user's choice. Mirrors
    :func:`apply_persisted_volume`; errors degrade to WARN.
    """
    state = load_state()
    target = state["brightness"]
    await asyncio.sleep(_APPLY_VOLUME_DELAY_S)
    for attempt in range(_APPLY_VOLUME_RETRIES + 1):
        if not gateway.esp32.device_connected:
            logger.info("control: device gone before brightness re-apply")
            return
        try:
            if await _send_brightness(gateway, target):
                logger.info("control: re-applied brightness=%d", target)
                return
        except Exception:
            logger.exception("control: brightness re-apply raised")
        if attempt < _APPLY_VOLUME_RETRIES:
            await asyncio.sleep(_APPLY_VOLUME_DELAY_S)
    logger.warning("control: brightness re-apply gave up after retries")


async def _send_led_color(gateway: "Gateway", r: int, g: int, b: int) -> bool:
    """Light all 12 LEDs one colour, scaled by the saved LED brightness.

    Uses ``set_all`` (not ``set_indicator``) so it does not re-arm the
    firmware's idle-settle auto-reset timer — that is what lets a colour
    persist instead of self-clearing ~60 s later.
    """
    brightness = load_state()["led"]["brightness"]
    scaled = _scale_rgb(r, g, b, brightness)
    _result, error = await gateway.esp32.call_tool(_SET_ALL_LEDS_TOOL, scaled)
    if error:
        logger.warning("control: led set_all failed: %s", error)
        return False
    return True


async def _send_led_clear(gateway: "Gateway") -> bool:
    """Turn all LEDs off. True on success."""
    _result, error = await gateway.esp32.call_tool(_CLEAR_LEDS_TOOL, {})
    if error:
        logger.warning("control: led clear failed: %s", error)
        return False
    return True


def _led_slot(slot: Any) -> str | None:
    """Validate a slot name, or None if it is not one of LED_SLOTS."""
    return slot if slot in LED_SLOTS else None


async def apply_led_state(gateway: "Gateway", slot: str) -> None:
    """Light the LEDs for one phase ``slot`` (best-effort, never raises).

    ``idle`` respects its on/off (off → clear); ``listening`` and
    ``hermes`` always show their colour. Used by the voice-turn
    orchestration and the on-connect / post-turn restore, so it must not
    raise — see :func:`_best_effort_device_call`.
    """
    led = load_state()["led"]
    cfg = led.get(slot, {})
    if slot == "idle" and not cfg.get("on", False):
        await _best_effort_device_call(gateway, _CLEAR_LEDS_TOOL, {}, "led:idle-off")
        return
    scaled = _scale_rgb(
        cfg.get("r", 0), cfg.get("g", 0), cfg.get("b", 0), led["brightness"]
    )
    await _best_effort_device_call(
        gateway, _SET_ALL_LEDS_TOOL, scaled, f"led:{slot}"
    )


async def set_led(
    gateway: "Gateway",
    slot: Any,
    *,
    on: Any = None,
    r: Any = 0,
    g: Any = 0,
    b: Any = 0,
) -> dict[str, Any]:
    """Persist one LED slot's colour (and, for ``idle``, on/off).

    ``idle`` is applied to the device immediately (unless a voice turn
    is in flight, where the turn owns the LED and the post-turn restore
    will pick up the new idle). ``listening`` / ``hermes`` are
    persisted only — they show during their phase or via
    :func:`preview_led`. Returns ``{"ok": True, "led": {...}}`` or
    ``{"ok": False, "error": ...}`` on a device-call failure.
    """
    name = _led_slot(slot)
    if name is None:
        return {"ok": False, "error": f"slot must be one of {list(LED_SLOTS)}"}
    state = load_state()
    led = state["led"]
    colour = {"r": _clamp_rgb(r), "g": _clamp_rgb(g), "b": _clamp_rgb(b)}
    if name == "idle":
        idle_on = bool(on) if on is not None else led["idle"]["on"]
        led["idle"] = {"on": idle_on, **colour}
        # Apply live unless a turn owns the LED right now.
        if not getattr(gateway, "voice_turn_active", False):
            ok = (
                await _send_led_color(gateway, **colour)
                if idle_on
                else await _send_led_clear(gateway)
            )
            if not ok:
                return {"ok": False, "error": "device call failed"}
    else:
        led[name] = colour
    state["led"] = led
    save_state(state)
    return {"ok": True, "led": led}


async def set_led_brightness(gateway: "Gateway", value: Any) -> dict[str, Any]:
    """Set the global LED brightness (0..100) and persist it.

    Scales every LED colour (idle/listening/hermes) sent to the device.
    Re-applies the idle colour live when idle is on and no voice turn
    owns the LED. Returns ``{"ok": True, "brightness": int}`` on success
    or ``{"ok": False, "error": ...}`` when the device call fails.
    """
    target = _clamp_led_brightness(value)
    state = load_state()
    state["led"]["brightness"] = target
    save_state(state)
    if not getattr(gateway, "voice_turn_active", False):
        idle = state["led"]["idle"]
        if idle["on"] and not await _send_led_color(
            gateway, idle["r"], idle["g"], idle["b"]
        ):
            return {"ok": False, "error": "device call failed"}
    return {"ok": True, "brightness": target}


async def preview_led(gateway: "Gateway", slot: Any) -> dict[str, Any]:
    """Flash a slot's colour for ~1.5 s, then revert to the idle state.

    Lets the dashboard show the listening / hermes colours, which would
    otherwise only appear for ~1 s mid-conversation. Refused while a
    voice turn owns the LED. Returns ``{"ok": True, "slot": ...}`` or an
    error dict.
    """
    name = _led_slot(slot)
    if name is None:
        return {"ok": False, "error": f"slot must be one of {list(LED_SLOTS)}"}
    if not gateway.esp32.device_connected:
        return {"ok": False, "error": "no device connected"}
    if getattr(gateway, "voice_turn_active", False):
        return {"ok": False, "error": "busy (voice turn active)"}
    await apply_led_state(gateway, name)
    await asyncio.sleep(LED_PREVIEW_SECONDS)
    await apply_led_state(gateway, "idle")
    return {"ok": True, "slot": name}


async def apply_persisted_led(gateway: "Gateway") -> None:
    """Re-apply the saved idle LED colour after a device (re)connects.

    The firmware boots with LEDs off, so only an "on" idle state needs
    re-asserting. Mirrors :func:`apply_persisted_volume`; errors degrade
    to WARN.
    """
    idle = load_state()["led"]["idle"]
    if not idle["on"]:
        return
    await asyncio.sleep(_APPLY_VOLUME_DELAY_S)
    for attempt in range(_APPLY_VOLUME_RETRIES + 1):
        if not gateway.esp32.device_connected:
            logger.info("control: device gone before LED re-apply")
            return
        try:
            if await _send_led_color(gateway, idle["r"], idle["g"], idle["b"]):
                logger.info(
                    "control: re-applied idle LED rgb=(%d,%d,%d)",
                    idle["r"], idle["g"], idle["b"],
                )
                return
        except Exception:
            logger.exception("control: LED re-apply raised")
        if attempt < _APPLY_VOLUME_RETRIES:
            await asyncio.sleep(_APPLY_VOLUME_DELAY_S)
    logger.warning("control: LED re-apply gave up after retries")


async def restore_idle_led(gateway: "Gateway") -> None:
    """Restore the idle LED after a voice turn (best-effort).

    Called from the hermes bridge's ``finally`` instead of a hard
    "LEDs off": re-lights the user's idle colour, or clears when idle is
    off. Never raises — a cosmetic restore must not break the turn
    teardown. Thin alias over :func:`apply_led_state` for the idle slot.
    """
    await apply_led_state(gateway, "idle")


async def set_head_angle(
    gateway: "Gateway", yaw: Any, pitch: Any
) -> dict[str, Any]:
    """Move the head live (not persisted) to clamped ``yaw``/``pitch``.

    Backs the dashboard joystick's POST /control/head. Mirrors
    :func:`set_mic_gain`: clamps the inputs (yaw ``[-90,90]``, pitch
    ``[5,85]``), calls the firmware ``set_head_angles`` device tool, and
    reports the connected flag so the caller can surface it. Returns
    ``{"ok": True, "yaw": int, "pitch": int, "connected": bool}`` on
    success or ``{"ok": False, "error": ..., ...}`` when the device call
    fails or no device is connected.
    """
    target_yaw = _clamp_head_yaw(yaw)
    target_pitch = _clamp_head_pitch(pitch)
    connected = bool(gateway.esp32.device_connected)
    if not connected:
        return {
            "ok": False,
            "error": "no device connected",
            "yaw": target_yaw,
            "pitch": target_pitch,
            "connected": False,
        }
    result, error = await gateway.esp32.call_tool(
        _SET_HEAD_ANGLES_TOOL, {"yaw": target_yaw, "pitch": target_pitch}
    )
    if error:
        logger.warning("control: set_head_angles failed: %s", error)
        return {
            "ok": False,
            "error": "device call failed",
            "yaw": target_yaw,
            "pitch": target_pitch,
            "connected": True,
        }
    return {
        "ok": True,
        "yaw": target_yaw,
        "pitch": target_pitch,
        "connected": True,
    }


async def set_neutral_pose(
    gateway: "Gateway", yaw: Any, pitch: Any
) -> dict[str, Any]:
    """Save the head's neutral (rest) pose; persisted to NVS on-device.

    Backs the dashboard's POST /control/neutral_pose. Same clamping and
    contract as :func:`set_head_angle`, but calls the firmware
    ``set_neutral_pose`` device tool (implemented in parallel on the
    firmware side) so the chosen pose survives reboots.
    """
    target_yaw = _clamp_head_yaw(yaw)
    target_pitch = _clamp_head_pitch(pitch)
    connected = bool(gateway.esp32.device_connected)
    if not connected:
        return {
            "ok": False,
            "error": "no device connected",
            "yaw": target_yaw,
            "pitch": target_pitch,
            "connected": False,
        }
    result, error = await gateway.esp32.call_tool(
        _SET_NEUTRAL_POSE_TOOL, {"yaw": target_yaw, "pitch": target_pitch}
    )
    if error:
        logger.warning("control: set_neutral_pose failed: %s", error)
        return {
            "ok": False,
            "error": "device call failed",
            "yaw": target_yaw,
            "pitch": target_pitch,
            "connected": True,
        }
    return {
        "ok": True,
        "yaw": target_yaw,
        "pitch": target_pitch,
        "connected": True,
    }


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


# ---- Mode presets (yorishiro fork) -----------------------------------
#
# A "mode" is a named snapshot of the dashboard-controllable settings the
# user can re-apply in one shot. The snapshot is assembled by the HTTP
# layer (which owns the device query for proximity and the heartbeat
# runner) and handed to :func:`save_preset`; :func:`apply_preset` re-sends
# it via the same setters the dashboard uses. Stored one JSON file per
# preset under ``~/.stackchan/presets`` (atomic write, same flavour as the
# control state file). The head's neutral pose is intentionally excluded —
# it depends on where the device is physically placed.


def _presets_dir() -> Path:
    return Path(
        os.getenv("STACKCHAN_PRESETS_DIR", "") or DEFAULT_PRESETS_PATH
    ).expanduser()


def normalize_preset_name(name: Any) -> str | None:
    """Validate a preset name, returning it trimmed or None if unsafe.

    The name doubles as a filename, so this rejects empties, over-long
    names, path separators, ``.``/``..``, leading dots and control
    characters to keep the file inside the presets directory (no
    traversal). Unicode (e.g. Japanese) names are allowed.
    """
    if not isinstance(name, str):
        return None
    trimmed = name.strip()
    if not trimmed or len(trimmed) > MAX_PRESET_NAME_LEN:
        return None
    if trimmed in (".", "..") or trimmed.startswith("."):
        return None
    if any(ch in trimmed for ch in ("/", "\\", "\x00")):
        return None
    if any(ord(ch) < 0x20 for ch in trimmed):
        return None
    return trimmed


def _preset_file(name: str) -> Path:
    return _presets_dir() / f"{name}.json"


def _sanitize_snapshot(snapshot: Any) -> dict[str, Any]:
    """Keep only the preset-relevant settings, clamped / validated.

    Mirrors :func:`save_state`'s clamping for the gateway-owned fields and
    validates the device-sourced proximity / heartbeat blocks. Drops
    everything else (e.g. heartbeat ``speak`` / ``interval_min``), so a
    preset only carries what :func:`apply_preset` knows how to restore.
    """
    snap = snapshot if isinstance(snapshot, dict) else {}
    out: dict[str, Any] = {}
    if "volume" in snap:
        out["volume"] = _clamp_volume(snap["volume"])
    if "muted" in snap:
        out["muted"] = bool(snap["muted"])
    if "pre_mute_volume" in snap:
        out["pre_mute_volume"] = _clamp_volume(snap["pre_mute_volume"])
    if "mic_gain" in snap:
        out["mic_gain"] = _clamp_mic_gain(snap["mic_gain"])
    if "brightness" in snap:
        out["brightness"] = _clamp_brightness(snap["brightness"])
    if "led" in snap:
        out["led"] = _normalize_led(snap["led"])
    prox = snap.get("proximity")
    if (
        isinstance(prox, dict)
        and prox.get("mode") in ("reflex", "listen", "off")
        and isinstance(prox.get("threshold"), int)
        and not isinstance(prox.get("threshold"), bool)
    ):
        out["proximity"] = {
            "mode": prox["mode"],
            "threshold": min(max(prox["threshold"], 0), 2047),
        }
    hb = snap.get("heartbeat")
    if isinstance(hb, dict) and isinstance(hb.get("gestures"), bool):
        out["heartbeat"] = {"gestures": hb["gestures"]}
    return out


async def save_preset(
    name: Any, snapshot: dict[str, Any], *, overwrite: bool = False
) -> dict[str, Any]:
    """Persist ``snapshot`` as a named preset (atomic write).

    Returns ``{"ok": True, "preset": name}`` or an error dict (invalid
    name / already exists / write failure).
    """
    safe = normalize_preset_name(name)
    if safe is None:
        return {"ok": False, "error": "invalid preset name"}
    path = _preset_file(safe)
    if path.exists() and not overwrite:
        return {"ok": False, "error": f"preset '{safe}' already exists"}
    payload = {
        "name": safe,
        "ts": time.time(),
        "settings": _sanitize_snapshot(snapshot),
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
        logger.warning("control: cannot write preset %s (%s)", path, exc)
        return {"ok": False, "error": "cannot write preset"}
    return {"ok": True, "preset": safe}


def load_preset(name: Any) -> dict[str, Any] | None:
    """Read a preset file, or None if missing / unreadable / invalid."""
    safe = normalize_preset_name(name)
    if safe is None:
        return None
    try:
        data = json.loads(_preset_file(safe).read_text("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("control: unreadable preset %s (%s)", safe, exc)
        return None
    return data if isinstance(data, dict) else None


async def list_presets() -> list[dict[str, Any]]:
    """List saved presets as ``[{"name", "ts"}]`` (name-sorted)."""
    directory = _presets_dir()
    if not directory.exists():
        return []
    out: list[dict[str, Any]] = []
    for file in sorted(directory.glob("*.json")):
        try:
            data = json.loads(file.read_text("utf-8"))
            out.append({"name": data.get("name", file.stem), "ts": data.get("ts")})
        except (OSError, ValueError):
            out.append({"name": file.stem, "ts": None})
    return out


async def delete_preset(name: Any) -> dict[str, Any]:
    """Delete a named preset file."""
    safe = normalize_preset_name(name)
    if safe is None:
        return {"ok": False, "error": "invalid preset name"}
    path = _preset_file(safe)
    if not path.exists():
        return {"ok": False, "error": f"preset '{safe}' not found"}
    try:
        path.unlink()
    except OSError as exc:
        logger.warning("control: cannot delete preset %s (%s)", path, exc)
        return {"ok": False, "error": "cannot delete preset"}
    return {"ok": True, "preset": safe}


async def apply_preset(gateway: "Gateway", name: Any) -> dict[str, Any]:
    """Re-apply a saved preset's settings to the device.

    Serialised with :data:`_preset_lock` and refused mid voice-turn.
    Re-sends each setting via the same setters the dashboard uses;
    per-setting failures are collected so a partial apply still reports
    what failed (``{"ok": False, "error": "partial apply", "failed": [...]}``).
    """
    safe = normalize_preset_name(name)
    if safe is None:
        return {"ok": False, "error": "invalid preset name"}
    async with _preset_lock:
        if getattr(gateway, "voice_turn_active", False):
            return {"ok": False, "error": "busy (voice turn active)"}
        data = load_preset(safe)
        if data is None:
            return {"ok": False, "error": f"preset '{safe}' not found"}
        settings = data.get("settings")
        if not isinstance(settings, dict):
            return {"ok": False, "error": "preset has no settings"}
        failed: list[str] = []

        # Volume (+ mute): restore the real level first, then mute on top
        # so a later unmute returns to the right place.
        if "volume" in settings or "muted" in settings:
            base = settings.get(
                "pre_mute_volume", settings.get("volume", DEFAULT_VOLUME)
            )
            if settings.get("muted"):
                if not (await set_volume(gateway, base)).get("ok"):
                    failed.append("volume")
                elif not (await mute(gateway)).get("ok"):
                    failed.append("mute")
            elif not (await set_volume(gateway, settings.get("volume", base))).get(
                "ok"
            ):
                failed.append("volume")

        if "mic_gain" in settings and not (
            await set_mic_gain(gateway, settings["mic_gain"])
        ).get("ok"):
            failed.append("mic_gain")

        if "brightness" in settings and not (
            await set_brightness(gateway, settings["brightness"])
        ).get("ok"):
            failed.append("brightness")

        led = settings.get("led")
        if isinstance(led, dict):
            if "brightness" in led and not (
                await set_led_brightness(gateway, led["brightness"])
            ).get("ok"):
                failed.append("led.brightness")
            for slot in LED_SLOTS:
                cfg = led.get(slot)
                if not isinstance(cfg, dict):
                    continue
                on = cfg.get("on") if slot == "idle" else None
                result = await set_led(
                    gateway,
                    slot,
                    on=on,
                    r=cfg.get("r", 0),
                    g=cfg.get("g", 0),
                    b=cfg.get("b", 0),
                )
                if not result.get("ok"):
                    failed.append(f"led.{slot}")

        prox = settings.get("proximity")
        if isinstance(prox, dict) and "mode" in prox and "threshold" in prox:
            _result, error = await gateway.esp32.call_tool(
                _SET_PROXIMITY_TOOL,
                {"mode": prox["mode"], "threshold": prox["threshold"]},
            )
            if error:
                failed.append("proximity")

        hb = settings.get("heartbeat")
        if isinstance(hb, dict) and "gestures" in hb:
            runner = getattr(gateway, "_heartbeat", None)
            if runner is not None:
                runner.set_gestures(bool(hb["gestures"]))

        if failed:
            return {
                "ok": False,
                "error": "partial apply",
                "failed": failed,
                "preset": safe,
            }
        return {"ok": True, "preset": safe, "applied": True}
