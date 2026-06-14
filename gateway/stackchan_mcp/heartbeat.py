"""Autonomous heartbeat: idle gestures plus notify-style speech.

yorishiro fork specific module (not intended for upstream PR) —
Phase D (gestures) + Phase E (notification speech).

Stage 1 (Phase D): the StackChan periodically performs a small, silent
gesture (a glance around, a brief expression change, a nod) so it
feels alive between conversations.

Stage 2 (Phase E): **notify-style** speech, strictly opt-in. Silence
is the default; the heartbeat only speaks when one of its information
sources has something worth saying — an active weather warning or a
rain forecast in the morning window (:mod:`stackchan_mcp.weather`), or
a note written today in the evening window (:mod:`stackchan_mcp.notes`).
It never produces small talk. The project's first design principle —
never interrupt the household's conversation — is enforced by layered
suppression: quiet hours, the shared audio-pipeline lock, an active
recording slot, a cooldown after any user interaction, and a daily
utterance cap. Future sources (e.g. SwitchBot motion / CO2 sensors)
should slot in as additional ``_check_*`` methods.

Environment variables (stage 1):

- ``STACKCHAN_HEARTBEAT_INTERVAL_MIN`` — average minutes between
  ticks. **Unset, zero or negative disables the heartbeat
  entirely** (the default; this feature is strictly opt-in).
- ``STACKCHAN_HEARTBEAT_JITTER`` — fractional jitter applied to each
  interval, ``0..0.9``. Defaults to ``0.25`` so ticks do not land
  on a metronomic schedule.
- ``STACKCHAN_HEARTBEAT_QUIET`` — quiet hours as ``"HH:MM-HH:MM"``
  (local time, midnight-crossing ranges allowed). Defaults to
  ``"22:00-06:30"``. Set to ``"off"`` to disable quiet hours.
- ``STACKCHAN_HEARTBEAT_GESTURES`` — set to ``0`` to disable the
  random idle gestures while keeping the tick (and speech) running.

Environment variables (stage 2, all inert unless SPEAK is set):

- ``STACKCHAN_HEARTBEAT_SPEAK`` — master switch; ``1`` enables
  notification speech. Unset/0 keeps stage-1 behaviour exactly.
- ``STACKCHAN_HEARTBEAT_SPEAK_COOLDOWN_MIN`` — minutes of silence
  after any user interaction (voice turn or touch). Default 20.
- ``STACKCHAN_HEARTBEAT_SPEAK_MAX_PER_DAY`` — daily utterance cap
  (safety valve). Default 3.
- ``STACKCHAN_WEATHER_AREA`` / ``STACKCHAN_WEATHER_CITY`` — JMA
  office code (e.g. ``270000``) and class20 municipality code (e.g.
  ``2720900`` for Moriguchi). Both required for the weather source.
- ``STACKCHAN_WEATHER_POP_THRESHOLD`` — precipitation-probability
  threshold (%), default 50.
- ``STACKCHAN_WEATHER_WINDOW`` — morning check window, default
  ``06:30-09:30``.
- ``STACKCHAN_MEMO_WINDOW`` — evening memo-reminder window, default
  ``18:00-21:00``.
- ``STACKCHAN_HEARTBEAT_STATE`` — state file remembering what was
  already said (per-day flags, reminded notes, daily count) across
  restarts. Default ``~/.stackchan/heartbeat_state.json``.

A scheduled tick is skipped (not rescheduled early) when any guard
fails: no ESP32 connected, the audio pipeline is busy (TTS playback or
an active listen capture — they share one lock), or local time falls
inside quiet hours.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as _dt
import json
import logging
import os
import random
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import notes, weather
from .audio_stream import is_recording

if TYPE_CHECKING:
    from .gateway import Gateway

logger = logging.getLogger(__name__)

DEFAULT_JITTER = 0.25
DEFAULT_QUIET = "22:00-06:30"

DEFAULT_SPEAK_COOLDOWN_MIN = 20.0
DEFAULT_SPEAK_MAX_PER_DAY = 3
DEFAULT_POP_THRESHOLD = 50
DEFAULT_WEATHER_WINDOW = "06:30-09:30"
DEFAULT_MEMO_WINDOW = "18:00-21:00"
DEFAULT_STATE_PATH = "~/.stackchan/heartbeat_state.json"

#: Longest memo snippet quoted in a reminder, and how many memos one
#: utterance may list.
MEMO_SNIPPET_CHARS = 60
MEMO_MAX_LISTED = 2


@dataclasses.dataclass
class SpeakConfig:
    """Stage-2 (notification speech) settings. Absent = speech off."""

    cooldown_min: float = DEFAULT_SPEAK_COOLDOWN_MIN
    max_per_day: int = DEFAULT_SPEAK_MAX_PER_DAY
    weather_area: str = ""
    weather_city: str = ""
    pop_threshold: int = DEFAULT_POP_THRESHOLD
    weather_window: tuple[_dt.time, _dt.time] | None = None
    memo_window: tuple[_dt.time, _dt.time] | None = None
    state_path: Path = dataclasses.field(
        default_factory=lambda: Path(DEFAULT_STATE_PATH).expanduser()
    )

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


def _memo_snippet(content: str) -> str:
    """First non-empty line of a note, clamped for one spoken breath."""
    for line in content.splitlines():
        line = line.strip().lstrip("#-* ").strip()
        if line:
            return line[:MEMO_SNIPPET_CHARS]
    return ""


def _env_number(name: str, default: float) -> float:
    """A numeric env var, warning and falling back on garbage."""
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("heartbeat: invalid %s=%r; using %s", name, raw, default)
        return default


def speak_config_from_env() -> SpeakConfig | None:
    """Stage-2 settings from the environment, or None when off.

    ``STACKCHAN_HEARTBEAT_SPEAK`` is the master switch; anything other
    than an explicit truthy value keeps speech completely disabled.
    Malformed window specs raise (same fail-loudly policy as
    STACKCHAN_HEARTBEAT_QUIET) — a typo must not silently change when
    the robot is allowed to talk.
    """
    if os.getenv("STACKCHAN_HEARTBEAT_SPEAK", "").strip().lower() not in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return None
    return SpeakConfig(
        cooldown_min=max(
            0.0,
            _env_number(
                "STACKCHAN_HEARTBEAT_SPEAK_COOLDOWN_MIN",
                DEFAULT_SPEAK_COOLDOWN_MIN,
            ),
        ),
        max_per_day=max(
            0,
            int(
                _env_number(
                    "STACKCHAN_HEARTBEAT_SPEAK_MAX_PER_DAY",
                    DEFAULT_SPEAK_MAX_PER_DAY,
                )
            ),
        ),
        weather_area=os.getenv("STACKCHAN_WEATHER_AREA", "").strip(),
        weather_city=os.getenv("STACKCHAN_WEATHER_CITY", "").strip(),
        pop_threshold=int(
            _env_number("STACKCHAN_WEATHER_POP_THRESHOLD", DEFAULT_POP_THRESHOLD)
        ),
        weather_window=parse_quiet_hours(
            os.getenv("STACKCHAN_WEATHER_WINDOW", DEFAULT_WEATHER_WINDOW)
        ),
        memo_window=parse_quiet_hours(
            os.getenv("STACKCHAN_MEMO_WINDOW", DEFAULT_MEMO_WINDOW)
        ),
        state_path=Path(
            os.getenv("STACKCHAN_HEARTBEAT_STATE", "") or DEFAULT_STATE_PATH
        ).expanduser(),
    )


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
        gestures: bool = True,
        speak: SpeakConfig | None = None,
    ):
        self._gateway = gateway
        self._interval_min = interval_min
        self._jitter = jitter
        self._quiet = quiet
        self._rng = rng or random.Random()
        self._gestures = gestures
        self._speak = speak
        self._state: dict[str, Any] = self._load_state() if speak else {}
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

        gestures = os.getenv("STACKCHAN_HEARTBEAT_GESTURES", "1").strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
        )

        return cls(
            gateway,
            interval_min=interval_min,
            jitter=jitter,
            quiet=quiet,
            gestures=gestures,
            speak=speak_config_from_env(),
        )

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.get_running_loop().create_task(self._loop())
        logger.info(
            "heartbeat: enabled, interval=%.1f min (jitter ±%d%%), quiet=%s, "
            "gestures=%s, speak=%s",
            self._interval_min,
            int(self._jitter * 100),
            "-".join(t.strftime("%H:%M") for t in self._quiet)
            if self._quiet
            else "off",
            "on" if self._gestures else "off",
            "on" if self._speak else "off",
        )

    @property
    def gestures_enabled(self) -> bool:
        """Whether random idle gestures fire on each tick (Phase F)."""
        return self._gestures

    def set_gestures(self, enabled: bool) -> None:
        """Toggle idle gestures at runtime (Phase F dashboard control).

        A thin public wrapper over ``_gestures``; the tick loop's own
        ``if self._gestures`` check (in :meth:`_loop`) picks this up on
        the next tick. Notification speech (:meth:`_tick_speak`) is
        unaffected.
        """
        self._gestures = bool(enabled)

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

    def _today(self) -> _dt.date:
        """Local date; split out for tests."""
        return _dt.date.today()

    def _monotonic(self) -> float:
        """Monotonic clock; split out for tests."""
        return time.monotonic()

    def _skip_reason(self) -> str | None:
        if not self._gateway.esp32.device_connected:
            return "no device connected"
        # A voice turn holds no tts_lock while it is in STT / Hermes
        # (the lock only covers TTS playback), so check the bridge's
        # own flag too — a heartbeat gesture must never land mid-turn
        # (design principle #1: never interrupt the conversation).
        if getattr(self._gateway, "voice_turn_active", False):
            return "voice turn active"
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
                if await self._tick_speak():
                    continue
                if self._gestures:
                    await self._perform_gesture()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The heartbeat must never take the gateway down; a
                # failed tick just waits for the next one.
                logger.exception("heartbeat: tick failed")

    # ---- stage 2: notification speech (Phase E) --------------------

    async def _tick_speak(self) -> bool:
        """Run one notification pass; True when something was spoken.

        Silence is the default. Sources are polled in priority order
        (weather first, memo second) and at most one line is spoken
        per tick. New sources (e.g. SwitchBot motion / CO2 sensors)
        plug in as further ``_check_*`` calls here.
        """
        if self._speak is None:
            return False
        reason = self._speak_skip_reason()
        if reason is not None:
            logger.info("heartbeat: speak suppressed (%s)", reason)
            return False
        line = await self._check_weather() or await self._check_memo()
        if not line:
            return False
        await self._perform_speak(line)
        return True

    def _speak_skip_reason(self) -> str | None:
        assert self._speak is not None
        if is_recording():
            return "recording active"
        last = getattr(self._gateway, "last_human_interaction_monotonic", None)
        if (
            last is not None
            and self._monotonic() - last < self._speak.cooldown_min * 60.0
        ):
            return "recent interaction"
        if self._spoken_today() >= self._speak.max_per_day:
            return "daily cap"
        return None

    def _spoken_today(self) -> int:
        if self._state.get("speak_count_date") != self._today().isoformat():
            return 0
        try:
            return int(self._state.get("speak_count", 0))
        except (TypeError, ValueError):
            return 0

    async def _check_weather(self) -> str | None:
        """Morning weather line, or None. One successful check a day."""
        cfg = self._speak
        assert cfg is not None
        if not cfg.weather_area or not cfg.weather_city:
            return None
        if not is_quiet(self._now(), cfg.weather_window):
            return None
        today = self._today()
        if self._state.get("weather_done") == today.isoformat():
            return None
        try:
            line = await weather.check_weather(
                cfg.weather_area,
                cfg.weather_city,
                cfg.pop_threshold,
                today=today,
            )
        except Exception as exc:
            # Leave the daily flag unset so the next tick inside the
            # window retries; a flaky fetch must not eat the day.
            logger.warning("heartbeat: weather check failed: %s", exc)
            return None
        self._state["weather_done"] = today.isoformat()
        self._save_state()
        if line is None:
            logger.info("heartbeat: weather checked, nothing to say")
        return line

    async def _check_memo(self) -> str | None:
        """Evening reminder for notes written today, or None.

        The daily flag is only set when a reminder is actually spoken,
        so a memo written later in the window is still picked up by a
        subsequent tick. Per-note (name, mtime) pairs are remembered
        across restarts so the same content is never read out twice.
        """
        cfg = self._speak
        assert cfg is not None
        if not is_quiet(self._now(), cfg.memo_window):
            return None
        today = self._today()
        if self._state.get("memo_done") == today.isoformat():
            return None
        listing = notes.list_notes()
        reminded = self._state.get("reminded")
        if not isinstance(reminded, dict):
            reminded = {}
        fresh: list[tuple[str, int]] = []
        for entry in listing.get("notes", []):
            name, mtime = entry.get("name"), entry.get("mtime")
            if not name or not isinstance(mtime, int):
                continue
            if _dt.date.fromtimestamp(mtime) != today:
                continue
            if reminded.get(name) == mtime:
                continue
            fresh.append((name, mtime))
        if not fresh:
            return None
        snippets = []
        for name, _ in fresh[:MEMO_MAX_LISTED]:
            try:
                content = notes.read_note(name).get("content", "")
            except ValueError:
                continue
            snippet = _memo_snippet(content)
            if snippet:
                snippets.append(snippet)
        if not snippets:
            return None
        listed = "』と『".join(snippets)
        # Prune entries for deleted notes so the state file cannot
        # grow without bound, then remember what we are about to say.
        current_names = {e.get("name") for e in listing.get("notes", [])}
        reminded = {k: v for k, v in reminded.items() if k in current_names}
        reminded.update(dict(fresh))
        self._state["reminded"] = reminded
        self._state["memo_done"] = today.isoformat()
        self._save_state()
        return f"今日のメモに『{listed}』ってあるよ"

    async def _perform_speak(self, text: str) -> None:
        # Lazy import keeps capture-only deployments free of the tts
        # extras (same pattern as hermes_bridge.handle_voice_turn).
        from .tts.orchestrator import synthesize_and_send

        logger.info("heartbeat: speak %r", text)
        await self._set_face("happy")
        try:
            await synthesize_and_send({"text": text}, gateway=self._gateway)
        finally:
            await self._set_face("idle")
        today = self._today().isoformat()
        if self._state.get("speak_count_date") != today:
            self._state["speak_count_date"] = today
            self._state["speak_count"] = 0
        self._state["speak_count"] = self._spoken_today() + 1
        self._save_state()

    def _load_state(self) -> dict[str, Any]:
        path = self._speak.state_path if self._speak else None
        if path is None:
            return {}
        try:
            data = json.loads(path.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning("heartbeat: unreadable state file %s (%s)", path, exc)
            return {}

    def _save_state(self) -> None:
        # Atomic write (write-temp + os.replace), same flavour as the
        # control state file (stackchan_mcp.control.save_state): a crash
        # mid-write must not truncate the day's speak-count / reminded
        # flags into garbage.
        assert self._speak is not None
        path = self._speak.state_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fp:
                    json.dump(self._state, fp, ensure_ascii=False)
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except OSError as exc:
            logger.warning("heartbeat: cannot write state file %s (%s)", path, exc)

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
