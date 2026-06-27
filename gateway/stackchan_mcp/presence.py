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
from collections.abc import Awaitable, Callable
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import presence_report, sensors
from .event_log import rotate_old_entries
from .heartbeat import is_quiet, parse_quiet_hours

if TYPE_CHECKING:
    from .gateway import Gateway

logger = logging.getLogger(__name__)

#: A state-change observer: ``cb(old, new)`` fired whenever the derived
#: state flips. May be sync or async (a coroutine is awaited); exceptions
#: are swallowed so an observer can never take the monitor down. Used by
#: the proactive speaker to react to meaningful transitions (e.g. a
#: returning resident or a morning wake), keeping observation mechanical
#: in the gateway and the wording in Hermes.
StateChangeCb = Callable[
    ["PresenceState", "PresenceState"], Awaitable[None] | None
]

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

#: Default directory for the daily self-diagnostic reports — one
#: Markdown + JSON pair per day. Like the log, opt-out via the env
#: (``STACKCHAN_PRESENCE_REPORT=off``).
DEFAULT_REPORT_DIR = "~/.stackchan/presence_reports"

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

# ---- object_raw static-presence augmentation -------------------------
# The embedded presence algorithm adapts a *motionless* occupant out
# within ~a minute (TPRESENCE decays to ~0), so a person sitting still at
# the sensor's working range stops refreshing occupancy and the room
# wrongly debounces to ABSENT. The raw thermopile (``object_raw``) keeps
# reading the body's warmth the whole time, but it also drifts with the
# sensor's ambient temperature, so a *fixed* object_raw threshold is
# unsafe. We instead track a live baseline of the empty-room object_raw
# and treat a sustained excess over it as presence. Margins are in raw
# object_raw LSB; calibrated from a room walk-through (2026-06-27): a 1 m
# seated occupant read +400..+745 over the contemporaneous empty baseline,
# while empty-room noise was ~+/-35 (sd). Ambient drift moved the empty
# baseline ~-510/degC, which is exactly why the comparison is to a live
# baseline rather than a constant.
#
# The baseline tracks **asymmetrically and always** (never frozen): fast
# DOWN toward any lower reading (a leaving occupant / ambient-driven empty
# drift), slow UP toward a higher reading (resists absorbing a still body).
# An early "freeze while occupied" design self-latched for days in a replay
# over 9.7 days of real log (a stale frozen baseline never caught up to the
# day/night object_raw swing); the slow-up tau bounds any false hold to
# ~tens of minutes while still spanning a still occupant's detection gaps.

#: object_raw excess (over baseline) to assert a *static* occupant.
#: ~4x the empty noise sd; the 1 m seat clears it 3-5x over.
OBJ_PRESENT_MARGIN = 150
#: Hysteresis floor: once armed, the static hold clears only when the
#: excess falls back under this — so a single noisy sample never flaps it.
OBJ_DISARM_MARGIN = 60
#: Baseline EMA weight when ``object_raw`` is BELOW the baseline (empty /
#: leaving / ambient cooling): fast, ~tau = 1/alpha polls -> ~60 s at a 5 s
#: poll, so a departure is recovered within ~a minute.
OBJ_BASELINE_ALPHA_DOWN = 0.08
#: Baseline EMA weight when ``object_raw`` is ABOVE the baseline (a warm
#: body in view): slow, ~tau ~40 min, so a motionless occupant is held
#: through detection gaps yet a persistent high is absorbed (no permanent
#: latch). This single tau is the hold-duration <-> false-latch trade-off.
OBJ_BASELINE_ALPHA_UP = 0.004


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


def _resolve_report_dir() -> Path | None:
    """Resolve the daily-report directory, or None when disabled.

    Three-value like :func:`_resolve_log_path`: unset -> default dir,
    empty/``"off"`` -> None (disabled), any other value -> that dir. The
    caller additionally gates this on the log being enabled (a report
    without a log has no data to aggregate).
    """
    raw = os.getenv("STACKCHAN_PRESENCE_REPORT")
    if raw is None:
        return Path(DEFAULT_REPORT_DIR).expanduser()
    stripped = raw.strip()
    if not stripped or stripped.lower() == "off":
        return None
    return Path(stripped).expanduser()


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (write-temp + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            fp.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


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
        report_dir: Path | None = None,
    ):
        self._gateway = gateway
        self._dispatch = dispatch
        self._poll_sec = max(1.0, poll_sec)
        self._absent_after_s = _clamp_absent_after(absent_after_s)
        self._sleep_window = _valid_window(sleep_window) or DEFAULT_SLEEP_WINDOW
        self._log_path = log_path
        self._report_dir = report_dir
        #: Date (ISO) whose daily report was already attempted this run, so
        #: the once-a-day write is not re-evaluated on every poll.
        self._last_report_date: str | None = None
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
        self._on_change: list[StateChangeCb] = []
        # object_raw static-presence augmentation (see module constants).
        # Baseline lazily initialised to the first reading; armed only by a
        # real embedded 'moving' detection so a slowly warming surface is
        # never latched as presence.
        self._obj_baseline: float | None = None
        self._obj_armed = False
        self._last_static_present = False

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
        log_path = _resolve_log_path()
        return cls(
            gateway,
            dispatch=dispatch,
            poll_sec=poll_sec,
            absent_after_s=config["absent_after_s"],
            sleep_window=config["sleep_window"],
            log_path=log_path,
            # A daily report needs the log to aggregate; gate it on logging.
            report_dir=_resolve_report_dir() if log_path is not None else None,
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

    def register_on_state_change(self, cb: StateChangeCb) -> None:
        """Subscribe to derived-state transitions (see :data:`StateChangeCb`).

        Callbacks fire only on an actual flip and only for poll-driven
        changes — a ``update_config`` recompute (dashboard threshold edit)
        is intentionally silent (``notify=False``) so re-tuning the sleep
        window never reads as a real "woke up" / "came home" event.
        """
        self._on_change.append(cb)

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
            # object_raw static-presence augmentation, for tuning/debug:
            # baseline = current empty-room estimate, static_present = the
            # raw thermopile is holding a motionless occupant this poll.
            "occupancy": {
                "obj_baseline": (
                    round(self._obj_baseline, 1)
                    if self._obj_baseline is not None
                    else None
                ),
                "obj_armed": self._obj_armed,
                "static_present": self._last_static_present,
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
        # notify=False: a dashboard re-tune is not a real occupancy event,
        # so it must not trigger a proactive "おかえり"/"おはよう".
        self._set_state(self._derive_state(), notify=False)
        return {
            "ok": True,
            "config": {
                "absent_after_s": self._absent_after_s,
                "sleep_window": self._sleep_window,
            },
        }

    # ---- self-diagnostic report -----------------------------------

    def _read_recent_records(self, *, days: int) -> list[dict[str, Any]]:
        """Read the last ``days`` of presence-log records (ts_unix filter).

        Mirrors :func:`event_log.rotate_old_entries`' keep logic: malformed
        lines, non-dicts and rows without a usable ``ts_unix`` are dropped.
        A disabled log (``_log_path`` None) or a missing file yields ``[]``.
        Disk errors are logged at WARNING and swallowed (the report degrades
        to empty rather than taking a request down).
        """
        path = self._log_path
        if path is None or not path.exists():
            return []
        cutoff = self._wall_clock() - days * 86400
        out: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for raw in f:
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    try:
                        obj = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    ts = obj.get("ts_unix")
                    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
                        continue
                    if ts >= cutoff:
                        out.append(obj)
        except (OSError, PermissionError) as exc:
            logger.warning("presence: cannot read log %s for report (%s)", path, exc)
        return out

    def build_report(self, *, days: int = 7) -> dict[str, Any]:
        """Aggregate the last ``days`` of presence log into a health report.

        Blocking (reads the JSONL log); the HTTP route runs it via
        ``asyncio.to_thread``. The current absent debounce is passed so the
        recommendation can compare against the live threshold. An empty /
        disabled log yields ``{"empty": True}``.
        """
        records = self._read_recent_records(days=days)
        return presence_report.build_report(
            records,
            now=self._wall_clock(),
            current_absent_after_s=self._absent_after_s,
        )

    def _today(self) -> _dt.date:
        """Current JST date (the report-boundary axis); split out for tests.

        JST-explicit so the day boundary follows the resident's clock
        regardless of the server timezone.
        """
        return _dt.datetime.now(presence_report.JST).date()

    def _maybe_write_daily_report(self) -> None:
        """Write today's diagnostic report once per day (poll-driven).

        Idempotent and restart-safe: the in-memory ``_last_report_date``
        skips the rebuild on every poll within a day, and an existing file
        skips a re-run after a restart. The report covers the last 24 h
        (``days=1``) and is named by the generation date. Fire-and-forget:
        a disabled report (``_report_dir`` None) is a no-op and any disk
        error is logged at WARNING and swallowed so the poll never dies.
        """
        if self._report_dir is None:
            return
        today = self._today().isoformat()
        if today == self._last_report_date:
            return
        self._last_report_date = today  # mark attempted regardless of outcome
        md_path = self._report_dir / f"{today}.md"
        if md_path.exists():
            return  # already written today (survives a restart)
        try:
            report = self.build_report(days=1)
            _atomic_write_text(md_path, presence_report.render_markdown(report))
            _atomic_write_text(
                self._report_dir / f"{today}.json",
                json.dumps(report, ensure_ascii=False, indent=2),
            )
            logger.info("presence: wrote daily report %s", md_path)
        except (OSError, PermissionError) as exc:
            logger.warning("presence: cannot write daily report %s (%s)", md_path, exc)

    # ---- internals -------------------------------------------------

    def _set_state(self, new: PresenceState, *, notify: bool = True) -> None:
        """Assign the derived state and fire observers on an actual flip.

        Observers run as a detached task (``create_task``) so the poll loop
        never waits on Hermes/TTS; with none registered this is a plain
        assignment (no task, no running-loop requirement) so the direct
        ``await monitor._poll_once()`` path in tests is unchanged.
        """
        old = self._state
        self._state = new
        if notify and new != old and self._on_change:
            asyncio.get_running_loop().create_task(self._fire_change(old, new))

    async def _fire_change(self, old: PresenceState, new: PresenceState) -> None:
        for cb in list(self._on_change):
            try:
                result = cb(old, new)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                # An observer must never take the monitor down (same
                # discipline as _loop): log and move on.
                logger.exception("presence: on_state_change callback failed")

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
            # object_raw augmentation (lets us tune margins from the log).
            "obj_baseline": (
                round(self._obj_baseline, 1)
                if self._obj_baseline is not None
                else None
            ),
            "obj_armed": self._obj_armed,
            "static_present": self._last_static_present,
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

    def _update_occupancy(self, snap: dict[str, Any]) -> bool:
        """Decide room occupancy from the embedded signal *and* object_raw.

        Returns True when the room should be treated as occupied this poll
        (the caller refreshes ``_last_present_mono``). Combines two signals:

        - **moving** — the embedded presence/motion detection
          (``snap['present']`` = ``pres_flag or presence > 200``). Fires on
          entry and any body movement; decays to False on a still occupant.
        - **static** — ``object_raw`` sustained above a slow empty-room
          baseline. Holds a motionless occupant the embedded signal drops.

        The static signal is gated on an **armed** flag set only by a real
        ``moving`` detection, so a gradually warming surface (sun on a wall,
        an appliance) — which never trips the embedded detector — can never
        latch the room as occupied. The baseline tracks ``object_raw``
        asymmetrically and always (never frozen): fast DOWN toward a lower
        reading (a leaving occupant / ambient-driven empty drift), slow UP
        toward a higher reading (so a motionless body is held through its
        detection gaps yet a persistent high is eventually absorbed — no
        permanent latch).

        Cold start: the baseline initialises to the first reading, so an
        occupant present *and still* at gateway start is held only once they
        move (which arms the static hold) or once they leave and return
        (which teaches the true empty baseline) — same as the pre-feature
        behaviour, never worse.
        """
        moving = bool(snap.get("present"))
        obj = snap.get("object_raw")
        if isinstance(obj, bool) or not isinstance(obj, (int, float)):
            # No usable raw signal: fall back to the embedded detection.
            self._last_static_present = False
            return moving
        obj = float(obj)
        if self._obj_baseline is None:
            self._obj_baseline = obj
        if moving:
            self._obj_armed = True
        excess = obj - self._obj_baseline
        if self._obj_armed and not moving and excess <= OBJ_DISARM_MARGIN:
            # Body signal clearly gone (object_raw fell back to baseline):
            # disarm so the next warm-but-static surface needs a fresh arming.
            self._obj_armed = False
        static_present = self._obj_armed and excess > OBJ_PRESENT_MARGIN
        self._last_static_present = static_present
        occupied = moving or static_present
        # Asymmetric, always-on baseline tracking. Fast toward lower
        # readings (recover from a departure / follow ambient), slow toward
        # higher readings (resist absorbing a still occupant, but absorb a
        # persistent high over ~tens of minutes so it can never latch).
        alpha = (
            OBJ_BASELINE_ALPHA_DOWN
            if obj < self._obj_baseline
            else OBJ_BASELINE_ALPHA_UP
        )
        self._obj_baseline += alpha * (obj - self._obj_baseline)
        return occupied

    async def _poll_once(self) -> None:
        """One poll: read TMOS, update the last-seen timestamp + state.

        Split from :meth:`_loop` so tests can drive ticks deterministically
        without spinning the asyncio sleep loop.
        """
        if not self._gateway.esp32.device_connected:
            # No device: cannot read. Stay UNKNOWN (fail-open) and force a
            # re-init on the next connect rather than flipping to ABSENT.
            self._set_state(PresenceState.UNKNOWN)
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
                self._set_state(PresenceState.UNKNOWN)
            logger.debug(
                "presence: read failed (%s), consec=%d", exc, self._consec_errors
            )
            return
        self._consec_errors = 0
        self._last_snapshot = snap
        if self._update_occupancy(snap):
            self._last_present_mono = self._monotonic()
        self._update_sleep_latch()
        self._set_state(self._derive_state())
        self._append_log(snap, self._state)
        self._maybe_write_daily_report()

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
