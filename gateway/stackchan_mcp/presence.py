"""Presence state machine + heartbeat occupancy gate (yorishiro fork).

yorishiro fork specific module (not intended for upstream PR) —
Phase D foundation for the life-support vision (morning greeting /
appliance auto-modes / proactive suggestions). This first slice wires
only the **heartbeat occupancy gate**: appliance control and BLE
identity ride on top later.

The TMOS PIR (Port A, behind the PaHUB2 mux on ch3) tells us whether
someone is in the room. This module polls it on a slow cadence and
derives a coarse state:

    UNKNOWN — not yet read / sensor erroring        (fail-open: heartbeat ON)
    ABSENT  — presence lost past the debounce window (heartbeat OFF)
    ACTIVE  — someone present, during waking hours    (heartbeat ON)
    QUIET   — sleeping hours with someone present, *or* a static sleeper
              held by the sleep latch through a detection gap (mode=off;
              the heartbeat is already suppressed by its own quiet-hours
              guard, so QUIET is a *mode* signal for future appliance
              control — the gate itself only ever suppresses on ABSENT)

The absent debounce (``absent_after_s``) is the *exit* timer of an
asymmetric hysteresis: entry is instant (any ``present`` read marks the
room occupied), exit is slow (presence must stay lost for the whole
debounce). Real TMOS logs show in-room gaps up to ~444 s while someone
sits still or drifts to the sensor's range edge, so the operational
debounce is minutes-scale (room data tunes it via the dashboard, not a
code constant). On top of that, the **sleep latch** bridges the one gap a
debounce cannot: a wholly still sleeper, undetectable for hours.

Design:

- **fail-open.** Occupancy must be *confident* to suppress the heartbeat,
  never to allow it. UNKNOWN (startup, dead sensor, no device) keeps the
  heartbeat running so a sensor fault never makes StackChan mute forever.
  Only a debounced ABSENT closes the gate. This upholds design principle
  #1 (never interrupt) without risking a silent robot.
- **Shares the I2C mux lock.** Reads go through :mod:`sensors`
  (``read_tmos``), so the poll and the dashboard sensor tab serialise on
  ``sensors._i2c_lock`` and never tear each other's mux selects.
- **Runtime-adjustable thresholds.** The absent debounce and the
  sleeping-hours window live in ``~/.stackchan/presence_state.json``
  (atomic write, ``control.save_state`` flavour) and are editable from the
  dashboard — values are not frozen in code (mirrors set_neutral_pose).
- **Opt-in.** Polling is off unless ``STACKCHAN_PRESENCE_POLL_SEC`` is set
  (>0). With it unset, ``from_env`` returns None, the gateway holds no
  monitor, and the heartbeat gate is a no-op (pre-feature behaviour).

Environment:

- ``STACKCHAN_PRESENCE_POLL_SEC`` — seconds between TMOS polls. **Unset,
  zero or negative disables presence monitoring entirely** (the default;
  opt-in like the heartbeat).
- ``STACKCHAN_PRESENCE_STATE`` — path to the persisted threshold file.
  Default ``~/.stackchan/presence_state.json``.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import tempfile
import time
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import sensors
from .event_log import rotate_old_entries
from .heartbeat import is_quiet, parse_quiet_hours

if TYPE_CHECKING:
    from .gateway import Gateway

logger = logging.getLogger(__name__)

#: Persisted threshold file. Overridable for tests via the env var.
DEFAULT_STATE_PATH = "~/.stackchan/presence_state.json"

#: Default poll cadence (only used once polling is opted in via the env).
DEFAULT_POLL_SEC = 10.0

#: Default absent debounce: how long presence must stay lost before the
#: room is declared ABSENT. A brief step out of the FoV must not flap the
#: gate, so this is minutes-scale, not the sub-second sensor latency.
DEFAULT_ABSENT_AFTER_S = 120

#: Default sleeping-hours window (matches the heartbeat's quiet default).
DEFAULT_SLEEP_WINDOW = "22:00-06:30"

#: Default presence log path. One JSONL line per poll (TMOS snapshot +
#: derived state) so we can later reconstruct occupancy gaps and tune the
#: absent debounce from real data rather than a guess.
DEFAULT_LOG_PATH = "~/.stackchan/presence_log.jsonl"

#: Presence log retention (days). Longer than the event-log default
#: (``event_log.RETENTION_DAYS`` = 7) because occupancy tuning wants
#: multi-week weekday/weekend patterns from the raw TMOS time series.
#: ~3.8 MB/day, so 28 days is ~110 MB — well within budget.
PRESENCE_RETENTION_DAYS = 28

#: Consecutive read failures before occupancy falls back to UNKNOWN
#: (fail-open: a dead sensor must not silence the heartbeat forever).
MAX_CONSEC_ERRORS = 3

#: Clamp bounds for the runtime-adjustable absent debounce.
MIN_ABSENT_AFTER_S = 5
MAX_ABSENT_AFTER_S = 3600


class PresenceState(str, Enum):
    """Coarse occupancy/mode derived from TMOS presence + wall clock."""

    UNKNOWN = "unknown"  # not yet read / sensor erroring (fail-open)
    ABSENT = "absent"  # presence lost past the debounce window
    ACTIVE = "active"  # someone present, waking hours
    QUIET = "quiet"  # someone present, sleeping hours


def _state_path() -> Path:
    return Path(
        os.getenv("STACKCHAN_PRESENCE_STATE", "") or DEFAULT_STATE_PATH
    ).expanduser()


def _resolve_log_path() -> Path | None:
    """Resolve the presence log path, or None when logging is disabled.

    - ``STACKCHAN_PRESENCE_LOG`` unset -> default path (logging on while the
      monitor runs).
    - Empty or ``"off"`` -> None (disabled).
    - Any other value -> that path (``~`` expanded).
    """
    raw = os.getenv("STACKCHAN_PRESENCE_LOG")
    if raw is None:
        return Path(DEFAULT_LOG_PATH).expanduser()
    stripped = raw.strip()
    if not stripped or stripped.lower() == "off":
        return None
    return Path(stripped).expanduser()


def _clamp_absent_after(value: Any) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return DEFAULT_ABSENT_AFTER_S
    return min(max(v, MIN_ABSENT_AFTER_S), MAX_ABSENT_AFTER_S)


def _valid_window(spec: Any) -> str | None:
    """Return ``spec`` trimmed if it parses as a window (or 'off'), else None."""
    if not isinstance(spec, str):
        return None
    try:
        parse_quiet_hours(spec)
    except ValueError:
        return None
    return spec.strip()


def load_config() -> dict[str, Any]:
    """Read the persisted thresholds, with defaults filled in.

    A missing or unreadable file yields the defaults rather than raising —
    the gateway must start even on a fresh host.
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
        logger.warning("presence: unreadable state file %s (%s)", path, exc)
    return {
        "absent_after_s": _clamp_absent_after(
            raw.get("absent_after_s", DEFAULT_ABSENT_AFTER_S)
        ),
        "sleep_window": _valid_window(raw.get("sleep_window")) or DEFAULT_SLEEP_WINDOW,
    }


def save_config(config: dict[str, Any]) -> None:
    """Persist the thresholds atomically (write-temp + os.replace)."""
    path = _state_path()
    payload = {
        "absent_after_s": _clamp_absent_after(config.get("absent_after_s")),
        "sleep_window": _valid_window(config.get("sleep_window")) or DEFAULT_SLEEP_WINDOW,
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
        logger.warning("presence: cannot write state file %s (%s)", path, exc)


class PresenceMonitor:
    """Polls TMOS occupancy and derives a coarse presence state.

    One instance per Gateway. The heartbeat reads
    :meth:`allows_heartbeat` so a proactive utterance only fires when
    someone is (or might be) in the room.
    """

    def __init__(
        self,
        gateway: "Gateway",
        *,
        dispatch: sensors.DispatchFn,
        poll_sec: float = DEFAULT_POLL_SEC,
        absent_after_s: int = DEFAULT_ABSENT_AFTER_S,
        sleep_window: str = DEFAULT_SLEEP_WINDOW,
        log_path: Path | None = None,
    ):
        self._gateway = gateway
        self._dispatch = dispatch
        self._poll_sec = max(1.0, poll_sec)
        self._absent_after_s = _clamp_absent_after(absent_after_s)
        self._sleep_window = _valid_window(sleep_window) or DEFAULT_SLEEP_WINDOW
        self._log_path = log_path
        self._quiet = parse_quiet_hours(self._sleep_window)
        self._state = PresenceState.UNKNOWN
        self._last_present_mono: float | None = None
        # Sleep latch: set once presence is confirmed inside the sleeping
        # window, cleared at waking hours. Holds QUIET through a static
        # sleeper's detection gap (see _update_sleep_latch).
        self._asleep = False
        self._last_snapshot: dict[str, Any] | None = None
        self._consec_errors = 0
        self._inited = False
        self._task: asyncio.Task[None] | None = None

    @classmethod
    def from_env(cls, gateway: "Gateway") -> "PresenceMonitor | None":
        """Build a monitor from environment, or None when disabled.

        Polling is opt-in: ``STACKCHAN_PRESENCE_POLL_SEC`` must be a
        positive number. Thresholds come from the persisted config file
        so a dashboard change survives a gateway restart.
        """
        raw = os.getenv("STACKCHAN_PRESENCE_POLL_SEC", "")
        try:
            poll_sec = float(raw) if raw else 0.0
        except ValueError:
            logger.warning(
                "presence: invalid STACKCHAN_PRESENCE_POLL_SEC=%r; disabled", raw
            )
            return None
        if poll_sec <= 0:
            return None

        async def dispatch(name: str, arguments: dict[str, Any]) -> list[Any]:
            # Lazy import avoids a gateway <- stdio_server import cycle and
            # mirrors how the HTTP control routes reach the device bus.
            from .stdio_server import _dispatch_mcp_tool

            return await _dispatch_mcp_tool(name, arguments, gateway)

        config = load_config()
        return cls(
            gateway,
            dispatch=dispatch,
            poll_sec=poll_sec,
            absent_after_s=config["absent_after_s"],
            sleep_window=config["sleep_window"],
            log_path=_resolve_log_path(),
        )

    def start(self) -> None:
        if self._task is not None:
            return
        if self._log_path is not None:
            # Prune entries older than the presence retention window once at
            # startup, reusing the event-log rotation (it keys on ``ts_unix``).
            rotate_old_entries(
                path=self._log_path, retention_days=PRESENCE_RETENTION_DAYS
            )
        self._task = asyncio.get_running_loop().create_task(self._loop())
        logger.info(
            "presence: enabled, poll=%.0fs, absent_after=%ds, sleep=%s",
            self._poll_sec,
            self._absent_after_s,
            self._sleep_window,
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

    # ---- gate + introspection -------------------------------------

    def allows_heartbeat(self) -> bool:
        """fail-open occupancy gate: suppress only when confidently ABSENT.

        UNKNOWN (startup / sensor fault / no device) and QUIET both allow
        — the heartbeat's own quiet-hours guard handles sleeping hours, so
        this gate's single job is to fall silent when the room is empty.
        """
        return self._state is not PresenceState.ABSENT

    @property
    def state(self) -> PresenceState:
        return self._state

    def snapshot(self) -> dict[str, Any]:
        """Summary for GET /control/presence and the status payload."""
        return {
            "enabled": True,
            "state": self._state.value,
            "asleep": self._asleep,  # QUIET internals: latched sleeper vs awake
            "allows_heartbeat": self.allows_heartbeat(),
            "last_seen_s_ago": self._last_seen_s_ago(),
            "poll_sec": self._poll_sec,
            "config": {
                "absent_after_s": self._absent_after_s,
                "sleep_window": self._sleep_window,
            },
            "tmos": self._last_snapshot,
        }

    def update_config(
        self,
        *,
        absent_after_s: Any = None,
        sleep_window: Any = None,
    ) -> dict[str, Any]:
        """Apply + persist new thresholds; recompute the state at once.

        Returns ``{"ok": True, "config": {...}}`` or an error dict for a
        malformed sleep window.
        """
        if absent_after_s is not None:
            self._absent_after_s = _clamp_absent_after(absent_after_s)
        if sleep_window is not None:
            win = _valid_window(sleep_window)
            if win is None:
                return {
                    "ok": False,
                    "error": "sleep_window must be 'HH:MM-HH:MM' or 'off'",
                }
            self._sleep_window = win
            self._quiet = parse_quiet_hours(win)
        save_config(
            {
                "absent_after_s": self._absent_after_s,
                "sleep_window": self._sleep_window,
            }
        )
        # Reflect the change immediately (e.g. a shorter debounce may flip
        # the room to ABSENT now, or a new window may switch ACTIVE<->QUIET).
        self._state = self._derive_state()
        return {
            "ok": True,
            "config": {
                "absent_after_s": self._absent_after_s,
                "sleep_window": self._sleep_window,
            },
        }

    # ---- internals -------------------------------------------------

    def _now(self) -> _dt.time:
        """Local wall-clock time; split out for tests."""
        return _dt.datetime.now().time()

    def _monotonic(self) -> float:
        """Monotonic clock; split out for tests."""
        return time.monotonic()

    def _wall_clock(self) -> float:
        """Wall-clock epoch seconds; split out for tests."""
        return time.time()

    def _last_seen_s_ago(self) -> float | None:
        """Seconds since presence was last detected, or None if never."""
        if self._last_present_mono is None:
            return None
        return round(self._monotonic() - self._last_present_mono, 1)

    def _append_log(self, snap: dict[str, Any], state: PresenceState) -> None:
        """Append one TMOS snapshot + derived state as a JSONL line.

        Fire-and-forget like :mod:`event_log`: any disk error is logged at
        WARNING and swallowed so logging can never take the monitor down.
        Disabled (``log_path`` None) is a no-op.
        """
        path = self._log_path
        if path is None:
            return
        line = {
            "ts_unix": self._wall_clock(),
            "state": state.value,
            "asleep": self._asleep,
            "last_seen_s_ago": self._last_seen_s_ago(),
            **snap,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
                f.flush()
        except (OSError, PermissionError) as exc:
            logger.warning("presence: cannot append log %s (%s)", path, exc)

    def _occupied(self) -> bool | None:
        """True=present (within debounce), False=absent, None=never seen."""
        if self._last_present_mono is None:
            return None
        return (self._monotonic() - self._last_present_mono) < self._absent_after_s

    def _update_sleep_latch(self) -> None:
        """Bridge a static sleeper through detection gaps (side effect).

        TMOS sees only *moving* warmth, so a wholly still sleeper goes
        undetected for the rest of the night — a gap no debounce can span.
        The latch records "presence was confirmed during the sleeping
        window" and is held until waking hours, so the room is not
        mis-declared ABSENT mid-sleep. An overnight outing never confirms
        presence in the window, so the latch stays clear and sleep vs.
        away-overnight remain distinguishable. Driven from the poll only;
        ``_derive_state`` stays a pure read of the current latch.
        """
        if not is_quiet(self._now(), self._quiet):
            self._asleep = False  # waking hours clear the latch
        elif self._occupied():  # confirmed present (None/False are falsy)
            self._asleep = True

    def _derive_state(self) -> PresenceState:
        occ = self._occupied()
        if occ is None:
            return PresenceState.UNKNOWN
        quiet_now = is_quiet(self._now(), self._quiet)
        if occ:
            return PresenceState.QUIET if quiet_now else PresenceState.ACTIVE
        # Debounce elapsed. In the sleeping window a set latch keeps QUIET
        # (a still sleeper whose motion fell below the sensor); otherwise
        # the room is genuinely empty.
        # [FUTURE Phase 3] a learned weekday/hour occupancy prior could
        # also lift ABSENT->QUIET/ACTIVE here once enough data accumulates.
        if quiet_now and self._asleep:
            return PresenceState.QUIET
        return PresenceState.ABSENT

    async def _poll_once(self) -> None:
        """One poll: read TMOS, update the last-seen timestamp + state.

        Split from :meth:`_loop` so tests can drive ticks deterministically
        without spinning the asyncio sleep loop.
        """
        if not self._gateway.esp32.device_connected:
            # No device: cannot read. Stay UNKNOWN (fail-open) and force a
            # re-init on the next connect rather than flipping to ABSENT.
            self._state = PresenceState.UNKNOWN
            self._consec_errors = 0
            self._inited = False
            return
        try:
            if not self._inited:
                await sensors.init_tmos(self._dispatch)
                self._inited = True
            snap = await sensors.read_tmos(self._dispatch)
        except sensors.SensorError as exc:
            self._consec_errors += 1
            # A failed read may mean the mux/channel dropped; re-init next
            # tick. Fall back to UNKNOWN (fail-open) after a few failures.
            self._inited = False
            if self._consec_errors >= MAX_CONSEC_ERRORS:
                self._state = PresenceState.UNKNOWN
            logger.debug(
                "presence: read failed (%s), consec=%d", exc, self._consec_errors
            )
            return
        self._consec_errors = 0
        self._last_snapshot = snap
        if snap.get("present"):
            self._last_present_mono = self._monotonic()
        self._update_sleep_latch()
        self._state = self._derive_state()
        self._append_log(snap, self._state)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._poll_sec)
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The monitor must never take the gateway down; a failed
                # tick just waits for the next one.
                logger.exception("presence: poll failed")
