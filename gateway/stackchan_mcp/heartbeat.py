"""Autonomous heartbeat: low-frequency, silent idle gestures.

yorishiro fork specific module (not intended for upstream PR) — Phase D.

The StackChan periodically performs a small, silent gesture (a glance
around, a brief expression change, a nod) so it feels alive between
conversations. Voice is deliberately out of scope for this stage: the
project's first design principle — never interrupt the household's
conversation — means the heartbeat must not make sound. A later
opt-in stage may add speech.

Environment variables:

- ``STACKCHAN_HEARTBEAT_INTERVAL_MIN`` — average minutes between
  gestures. **Unset, zero or negative disables the heartbeat
  entirely** (the default; this feature is strictly opt-in).
- ``STACKCHAN_HEARTBEAT_JITTER`` — fractional jitter applied to each
  interval, ``0..0.9``. Defaults to ``0.25`` so gestures do not land
  on a metronomic schedule.
- ``STACKCHAN_HEARTBEAT_QUIET`` — quiet hours as ``"HH:MM-HH:MM"``
  (local time, midnight-crossing ranges allowed). Defaults to
  ``"22:00-08:00"``. Set to ``"off"`` to disable quiet hours.

A scheduled tick is skipped (not rescheduled early) when any guard
fails: no ESP32 connected, the audio pipeline is busy (TTS playback or
an active listen capture — they share one lock), or local time falls
inside quiet hours.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import random
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .gateway import Gateway

logger = logging.getLogger(__name__)

DEFAULT_JITTER = 0.25
DEFAULT_QUIET = "22:00-08:00"

#: M5Stack-recommended servo operating range (matches the move_head
#: MCP tool validation in stdio_server.py).
YAW_MIN, YAW_MAX = -90, 90
PITCH_MIN, PITCH_MAX = 5, 85


def parse_quiet_hours(spec: str) -> tuple[_dt.time, _dt.time] | None:
    """Parse ``"HH:MM-HH:MM"`` into a (start, end) pair.

    Returns None for ``"off"`` / empty (quiet hours disabled). Raises
    ValueError on malformed input so a typo fails loudly at startup
    instead of silently running through the night.
    """
    spec = spec.strip()
    if not spec or spec.lower() == "off":
        return None
    try:
        start_s, end_s = spec.split("-", 1)
        start = _dt.time.fromisoformat(start_s.strip())
        end = _dt.time.fromisoformat(end_s.strip())
    except ValueError as exc:
        raise ValueError(
            f"STACKCHAN_HEARTBEAT_QUIET must be 'HH:MM-HH:MM' or 'off', got {spec!r}"
        ) from exc
    return start, end


def is_quiet(now: _dt.time, quiet: tuple[_dt.time, _dt.time] | None) -> bool:
    """True when ``now`` falls inside the quiet range.

    A range whose start is later than its end crosses midnight
    (e.g. 22:00-08:00 covers 23:00 and 03:00 but not 12:00).
    """
    if quiet is None:
        return False
    start, end = quiet
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def compute_delay_s(
    interval_min: float, jitter: float, rng: random.Random
) -> float:
    """Next sleep in seconds: interval ± jitter, never below 10 s."""
    factor = rng.uniform(1.0 - jitter, 1.0 + jitter)
    return max(10.0, interval_min * 60.0 * factor)


class HeartbeatRunner:
    """Owns the periodic gesture task. One instance per Gateway."""

    def __init__(
        self,
        gateway: "Gateway",
        *,
        interval_min: float,
        jitter: float = DEFAULT_JITTER,
        quiet: tuple[_dt.time, _dt.time] | None = None,
        rng: random.Random | None = None,
    ):
        self._gateway = gateway
        self._interval_min = interval_min
        self._jitter = jitter
        self._quiet = quiet
        self._rng = rng or random.Random()
        self._task: asyncio.Task[None] | None = None

    @classmethod
    def from_env(cls, gateway: "Gateway") -> "HeartbeatRunner | None":
        """Build a runner from environment, or None when disabled."""
        raw = os.getenv("STACKCHAN_HEARTBEAT_INTERVAL_MIN", "")
        try:
            interval_min = float(raw) if raw else 0.0
        except ValueError:
            logger.warning(
                "heartbeat: invalid STACKCHAN_HEARTBEAT_INTERVAL_MIN=%r; disabled",
                raw,
            )
            return None
        if interval_min <= 0:
            return None

        jitter_raw = os.getenv("STACKCHAN_HEARTBEAT_JITTER", "")
        try:
            jitter = float(jitter_raw) if jitter_raw else DEFAULT_JITTER
        except ValueError:
            jitter = DEFAULT_JITTER
        jitter = min(max(jitter, 0.0), 0.9)

        quiet = parse_quiet_hours(
            os.getenv("STACKCHAN_HEARTBEAT_QUIET", DEFAULT_QUIET)
        )
        return cls(gateway, interval_min=interval_min, jitter=jitter, quiet=quiet)

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.get_running_loop().create_task(self._loop())
        logger.info(
            "heartbeat: enabled, interval=%.1f min (jitter ±%d%%), quiet=%s",
            self._interval_min,
            int(self._jitter * 100),
            "-".join(t.strftime("%H:%M") for t in self._quiet)
            if self._quiet
            else "off",
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    # ---- internals -------------------------------------------------

    def _now(self) -> _dt.time:
        """Local wall-clock time; split out for tests."""
        return _dt.datetime.now().time()

    def _skip_reason(self) -> str | None:
        if not self._gateway.esp32.device_connected:
            return "no device connected"
        if self._gateway.esp32.tts_lock.locked():
            return "audio pipeline busy"
        if is_quiet(self._now(), self._quiet):
            return "quiet hours"
        return None

    async def _loop(self) -> None:
        while True:
            delay = compute_delay_s(self._interval_min, self._jitter, self._rng)
            logger.debug("heartbeat: next tick in %.0f s", delay)
            await asyncio.sleep(delay)
            reason = self._skip_reason()
            if reason is not None:
                logger.info("heartbeat: tick skipped (%s)", reason)
                continue
            try:
                await self._perform_gesture()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The heartbeat must never take the gateway down; a
                # failed gesture just waits for the next tick.
                logger.exception("heartbeat: gesture failed")

    async def _perform_gesture(self) -> None:
        gestures = [
            self._gesture_glance,
            self._gesture_expression,
            self._gesture_nod,
        ]
        gesture = self._rng.choice(gestures)
        logger.info("heartbeat: gesture %s", gesture.__name__)
        await gesture()

    async def _call(self, name: str, args: dict[str, Any]) -> Any:
        """Call an ESP32 device tool, returning result or None on error."""
        result, error = await self._gateway.esp32.call_tool(name, args)
        if error:
            logger.warning("heartbeat: %s failed: %s", name, error)
            return None
        return result

    async def _read_head_angles(self) -> tuple[int, int] | None:
        """Current (yaw, pitch), or None when unreadable.

        Gestures restore the head to where it was; when the angles
        cannot be read we do not move the head at all rather than
        yanking it to an arbitrary neutral pose.
        """
        result = await self._call("self.robot.get_head_angles", {})
        if not isinstance(result, dict):
            return None
        payload: Any = result
        content = result.get("content")
        if isinstance(content, list) and content:
            text = content[0].get("text", "") if isinstance(content[0], dict) else ""
            try:
                payload = json.loads(text)
            except (ValueError, TypeError):
                return None
        try:
            yaw = int(payload["yaw"])
            pitch = int(payload["pitch"])
        except (KeyError, TypeError, ValueError):
            return None
        return yaw, pitch

    async def _move_head(self, yaw: int, pitch: int) -> None:
        yaw = min(max(yaw, YAW_MIN), YAW_MAX)
        pitch = min(max(pitch, PITCH_MIN), PITCH_MAX)
        await self._call("self.robot.set_head_angles", {"yaw": yaw, "pitch": pitch})

    async def _set_face(self, face: str) -> None:
        await self._call("self.display.set_avatar", {"face": face})

    async def _gesture_glance(self) -> None:
        """Look to one side, then the other, then back. Curious face."""
        home = await self._read_head_angles()
        await self._set_face("thinking")
        if home is not None:
            yaw, pitch = home
            side = self._rng.choice((-1, 1))
            await self._move_head(yaw + 25 * side, pitch)
            await asyncio.sleep(1.2)
            await self._move_head(yaw - 20 * side, pitch)
            await asyncio.sleep(1.2)
            await self._move_head(yaw, pitch)
        else:
            await asyncio.sleep(2.0)
        await self._set_face("idle")

    async def _gesture_expression(self) -> None:
        """A brief flash of mood, then back to idle."""
        face = self._rng.choice(("happy", "surprised", "thinking"))
        await self._set_face(face)
        await asyncio.sleep(2.5)
        await self._set_face("idle")

    async def _gesture_nod(self) -> None:
        """A small nod (or two), keeping the current heading."""
        home = await self._read_head_angles()
        if home is None:
            return
        yaw, pitch = home
        for _ in range(self._rng.choice((1, 2))):
            await self._move_head(yaw, pitch - 12)
            await asyncio.sleep(0.5)
            await self._move_head(yaw, pitch)
            await asyncio.sleep(0.4)
