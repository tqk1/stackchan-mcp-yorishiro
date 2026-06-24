"""Proactive speaker: state-transition-driven autonomous speech (Phase D core).

yorishiro fork specific module (not intended for upstream PR) — the
"Hermes 自発判断層".

:mod:`presence` observes occupancy *mechanically* and fires
``register_on_state_change`` on a real flip. This module is the judgment
half: when a **meaningful** transition lands — someone returning to an
empty room (ABSENT→ACTIVE), or a morning wake out of the sleep latch
(QUIET→ACTIVE) — it asks Hermes for one short spoken line and plays it.
Observation stays mechanical in the gateway; the wording is Hermes' alone
(design principles #3/#4: Hermes is never modified, only triggered over
its existing ``/v1/chat/completions`` endpoint).

Design principle #1 — never interrupt the household's conversation — is
enforced by the same layered suppression the heartbeat uses: an active
voice turn, a multi-turn continuation gap, the shared audio lock, an
active recording, quiet hours, a cooldown after any user interaction, and
a daily cap. On top of that a per-transition **refire cooldown** means
sensor flapping (ABSENT⇄ACTIVE while someone lingers at the FoV edge)
speaks at most once.

Why a separate module from the heartbeat: the heartbeat is timer-driven
(it wakes itself on an interval); this is event-driven (it only ever runs
inside a presence callback). The two share guards by *copying* the
conditions rather than refactoring the heartbeat's instance-coupled
guards, so the existing heartbeat tests stay untouched.

Opt-in: with ``STACKCHAN_PROACTIVE`` unset the gateway builds no speaker
and nothing fires (pre-feature behaviour). The dashboard toggle
(``control.proactive_enabled``) gates it further at runtime, mirroring the
Hermes-pin routing toggle.

Environment variables (all inert unless STACKCHAN_PROACTIVE is set):

- ``STACKCHAN_PROACTIVE`` — master switch; ``1``/``true``/``yes``/``on``
  enables. Unset/anything else keeps the speaker absent.
- ``STACKCHAN_PROACTIVE_COOLDOWN_MIN`` — minutes of silence after any user
  interaction (voice turn or touch). Default 20.
- ``STACKCHAN_PROACTIVE_MAX_PER_DAY`` — daily utterance cap (safety
  valve). Default 4.
- ``STACKCHAN_PROACTIVE_REFIRE_MIN`` — minimum minutes between two
  utterances for the *same* transition kind (anti-chatter). Default 10.
- ``STACKCHAN_PROACTIVE_TRANSITIONS`` — comma-separated transition keys to
  enable. Default ``absent_active,quiet_active,active_quiet``.
- ``STACKCHAN_PROACTIVE_QUIET`` — quiet hours ``"HH:MM-HH:MM"`` / ``"off"``.
  Default ``"22:00-06:30"``.
- ``STACKCHAN_PROACTIVE_DAY_PRESET`` — mode preset applied when the room
  becomes active (wake / return home). Default ``"つうじょう"``. Empty
  disables the day-mode switch (greeting only).
- ``STACKCHAN_PROACTIVE_NIGHT_PRESET`` — mode preset applied when the room
  goes quiet for the night. Default ``"おやすみ"``. Empty disables the
  night-mode switch (greeting only).
- ``STACKCHAN_PROACTIVE_MODE_DELAY_S`` — seconds of pause between the
  greeting and the mode switch (a natural beat). Default 1.5. ``0`` makes
  the switch immediate.
- ``STACKCHAN_PROACTIVE_STATE`` — daily-count state file. Default
  ``~/.stackchan/proactive_state.json``.

Mode auto-switch: each speaking transition can also re-apply one of the
dashboard mode presets (the same ``おやすみ``/``つうじょう`` cards the user
saves). The greeting and the mode switch are ordered so a mute never
swallows the line: going quiet for the night speaks *then* applies the
muting night preset; becoming active applies the un-muting day preset
*then* speaks. The mode switch is best-effort — a missing preset is logged
and the greeting still plays.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as _dt
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .audio_stream import is_recording
from .heartbeat import is_quiet, parse_quiet_hours
from .presence import PresenceState

if TYPE_CHECKING:
    from .gateway import Gateway

logger = logging.getLogger(__name__)

DEFAULT_COOLDOWN_MIN = 20.0
DEFAULT_MAX_PER_DAY = 4
DEFAULT_REFIRE_MIN = 10.0
DEFAULT_QUIET = "22:00-06:30"
DEFAULT_TRANSITIONS = "absent_active,quiet_active,active_quiet"
DEFAULT_STATE_PATH = "~/.stackchan/proactive_state.json"

#: Seconds of pause between the greeting and the mode switch. A beat makes
#: the transition feel natural — the line lands, then the screen dims (or
#: brightens) rather than snapping the instant the speech ends. Tuned on
#: hardware (ケンジ feedback 2026-06-24: an immediate mute felt abrupt).
DEFAULT_MODE_DELAY_S = 1.5

#: Mode presets re-applied on the day/night transitions. These match the
#: dashboard "モード" cards the user saves; overridable via env so a rename
#: (or an empty string to disable a side of the switch) needs no code change.
DEFAULT_DAY_PRESET = "つうじょう"
DEFAULT_NIGHT_PRESET = "おやすみ"

#: System prompt for a proactive utterance. Deliberately *not* the voice
#: chat prompt (DEFAULT_VOICE_SYSTEM_PROMPT): no question, exactly one
#: line — this is a greeting, not the start of a conversation.
PROACTIVE_SYSTEM_PROMPT = (
    "あなたは小型ロボット「スタックチャン」です。"
    "これから伝える状況に対して、短く自然に一言だけ声をかけてください。"
    "質問はせず、1文だけにしてください。記号・絵文字・箇条書きは使わないでください。"
)


@dataclasses.dataclass(frozen=True)
class _Transition:
    """A presence transition worth speaking on + its situation framing.

    ``situation`` is person-agnostic on purpose: TMOS detects occupancy,
    not identity (who is in the room needs BLE, a later phase), so the
    line never assumes *which* resident triggered it.

    ``preset_role`` ("day"/"night"/None) selects which mode preset to
    re-apply alongside the greeting; ``preset_first`` orders the apply
    relative to the speech so a muting preset never swallows the line.
    ``exempt_quiet_hours`` lets the night greeting fire *inside* the quiet
    window (``おやすみ`` *is* the night line — suppressing it would be
    self-defeating).
    """

    key: str
    src: PresenceState
    dst: PresenceState
    situation: str
    preset_role: str | None = None
    preset_first: bool = True
    exempt_quiet_hours: bool = False


#: All transitions the speaker knows how to frame. ``from_env`` selects a
#: subset via STACKCHAN_PROACTIVE_TRANSITIONS. None has UNKNOWN as ``src``,
#: so a startup / reconnect ``UNKNOWN→ACTIVE`` never matches — the speaker
#: cannot greet on a gateway restart.
_ALL_TRANSITIONS: tuple[_Transition, ...] = (
    _Transition(
        "absent_active",
        PresenceState.ABSENT,
        PresenceState.ACTIVE,
        "誰もいなかった部屋に人が戻ってきました",
        preset_role="day",  # restore the un-muting day mode, then greet
        preset_first=True,
    ),
    _Transition(
        "quiet_active",
        PresenceState.QUIET,
        PresenceState.ACTIVE,
        "朝になって、部屋の人が起きて活動を始めたようです",
        preset_role="day",  # un-mute / brighten first, then "おはよう"
        preset_first=True,
    ),
    _Transition(
        "active_quiet",
        PresenceState.ACTIVE,
        PresenceState.QUIET,
        "夜になって、そろそろ就寝の時間のようです",
        preset_role="night",  # speak "おやすみ" first, *then* mute / dim
        preset_first=False,
        exempt_quiet_hours=True,  # fires at the start of the quiet window
    ),
)


def _env_number(name: str, default: float) -> float:
    """A numeric env var, warning and falling back on garbage."""
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("proactive: invalid %s=%r; using %s", name, raw, default)
        return default


def _parse_transitions(raw: str) -> set[str]:
    """Keys from a CSV, intersected with the known transitions."""
    requested = {p.strip().lower() for p in raw.split(",") if p.strip()}
    valid = {t.key for t in _ALL_TRANSITIONS}
    return requested & valid


@dataclasses.dataclass
class ProactiveConfig:
    """Proactive-speech settings (built by :meth:`ProactiveSpeaker.from_env`)."""

    cooldown_min: float = DEFAULT_COOLDOWN_MIN
    max_per_day: int = DEFAULT_MAX_PER_DAY
    refire_min: float = DEFAULT_REFIRE_MIN
    enabled_transitions: set[str] = dataclasses.field(
        default_factory=lambda: _parse_transitions(DEFAULT_TRANSITIONS)
    )
    quiet: tuple[_dt.time, _dt.time] | None = None
    day_preset: str = DEFAULT_DAY_PRESET
    night_preset: str = DEFAULT_NIGHT_PRESET
    mode_switch_delay_s: float = DEFAULT_MODE_DELAY_S
    state_path: Path = dataclasses.field(
        default_factory=lambda: Path(DEFAULT_STATE_PATH).expanduser()
    )


class ProactiveSpeaker:
    """Reacts to presence transitions with a single Hermes-authored line.

    One instance per Gateway, subscribed to the presence monitor via
    :meth:`PresenceMonitor.register_on_state_change`. Holds no task of its
    own — :meth:`on_state_change` is the entire surface.
    """

    def __init__(self, gateway: "Gateway", *, config: ProactiveConfig):
        self._gateway = gateway
        self._config = config
        self._state: dict[str, Any] = self._load_state()
        #: monotonic time of the last utterance per transition key, in
        #: memory only — a refire window is short-term anti-chatter and
        #: monotonic clocks do not survive a restart anyway.
        self._last_fire_mono: dict[str, float] = {}

    @classmethod
    def from_env(cls, gateway: "Gateway") -> "ProactiveSpeaker | None":
        """Build a speaker from the environment, or None when disabled.

        ``STACKCHAN_PROACTIVE`` is the master switch; anything other than
        an explicit truthy value keeps proactive speech completely off.
        A malformed quiet window raises (same fail-loudly policy as the
        heartbeat) — a typo must not silently change when the robot talks.
        """
        if os.getenv("STACKCHAN_PROACTIVE", "").strip().lower() not in (
            "1",
            "true",
            "yes",
            "on",
        ):
            return None
        config = ProactiveConfig(
            cooldown_min=max(
                0.0, _env_number("STACKCHAN_PROACTIVE_COOLDOWN_MIN", DEFAULT_COOLDOWN_MIN)
            ),
            max_per_day=max(
                0, int(_env_number("STACKCHAN_PROACTIVE_MAX_PER_DAY", DEFAULT_MAX_PER_DAY))
            ),
            refire_min=max(
                0.0, _env_number("STACKCHAN_PROACTIVE_REFIRE_MIN", DEFAULT_REFIRE_MIN)
            ),
            enabled_transitions=_parse_transitions(
                os.getenv("STACKCHAN_PROACTIVE_TRANSITIONS", DEFAULT_TRANSITIONS)
            ),
            quiet=parse_quiet_hours(
                os.getenv("STACKCHAN_PROACTIVE_QUIET", DEFAULT_QUIET)
            ),
            day_preset=os.getenv(
                "STACKCHAN_PROACTIVE_DAY_PRESET", DEFAULT_DAY_PRESET
            ).strip(),
            night_preset=os.getenv(
                "STACKCHAN_PROACTIVE_NIGHT_PRESET", DEFAULT_NIGHT_PRESET
            ).strip(),
            mode_switch_delay_s=max(
                0.0, _env_number("STACKCHAN_PROACTIVE_MODE_DELAY_S", DEFAULT_MODE_DELAY_S)
            ),
            state_path=Path(
                os.getenv("STACKCHAN_PROACTIVE_STATE", "") or DEFAULT_STATE_PATH
            ).expanduser(),
        )
        return cls(gateway, config=config)

    # ---- callback --------------------------------------------------

    async def on_state_change(
        self, old: PresenceState, new: PresenceState
    ) -> None:
        """Presence transition observer (see :data:`presence.StateChangeCb`).

        Runs the full guard gauntlet, then asks Hermes for the wording and
        speaks it. Any failure leaves StackChan silent (never raises into
        the monitor's fire-and-forget task).
        """
        # Runtime kill switch (dashboard), read live like the routing pin.
        from . import control

        if not control.proactive_enabled():
            return
        transition = self._match_transition(old, new)
        if transition is None:
            return
        reason = self._skip_reason(transition)
        if reason is not None:
            logger.info("proactive: skipped (%s) for %s", reason, transition.key)
            return
        if self._refire_suppressed(transition.key):
            logger.info("proactive: refire cooldown for %s", transition.key)
            return

        # Stamp the refire window up front so a second transition arriving
        # mid-action (sensor flap, or a mode apply still in flight) is already
        # suppressed — covers both the mode switch and the greeting.
        self._last_fire_mono[transition.key] = self._monotonic()

        # Order the mode switch around the speech so a muting preset never
        # swallows the line: day transitions un-mute first then greet; the
        # night transition speaks "おやすみ" first then mutes / dims. A short
        # beat between the two (only when a preset actually applies) keeps it
        # from snapping the instant the speech ends — ケンジ found an
        # immediate mute jarring (2026-06-24).
        preset = self._preset_for(transition)
        if transition.preset_first:
            await self._apply_mode(transition)
            await self._mode_switch_pause(preset)
            await self._speak(transition)
        else:
            await self._speak(transition)
            await self._mode_switch_pause(preset)
            await self._apply_mode(transition)

    async def _mode_switch_pause(self, preset: str) -> None:
        """A natural beat between the greeting and the mode switch.

        No-op when no preset applies (greeting-only transition) or the delay
        is configured to zero.
        """
        if preset and self._config.mode_switch_delay_s > 0:
            await asyncio.sleep(self._config.mode_switch_delay_s)

    async def _speak(self, transition: _Transition) -> None:
        """Ask Hermes for one line and play it (best-effort, never raises).

        A dead/empty Hermes leaves StackChan silent; the accompanying mode
        switch (already done or about to be) still stands.
        """
        from .hermes_bridge import ask_hermes

        situation = self._situation(transition)
        try:
            line = await ask_hermes(situation, system_prompt=PROACTIVE_SYSTEM_PROMPT)
        except Exception as exc:
            logger.warning("proactive: Hermes call failed (%s); staying silent", exc)
            return
        await self._perform_speak(line, transition.key)

    async def _apply_mode(self, transition: _Transition) -> None:
        """Re-apply the transition's mode preset (best-effort, never raises).

        Resolves the abstract role (day/night) to the configured preset
        name; an empty name disables that side of the switch, and a missing
        preset is logged without blocking the greeting.
        """
        name = self._preset_for(transition)
        if not name:
            return
        from . import control

        try:
            result = await control.apply_preset(self._gateway, name)
        except Exception as exc:  # defensive: apply_preset shouldn't raise
            logger.warning("proactive: apply_preset(%r) raised (%s)", name, exc)
            return
        if not result.get("ok"):
            logger.warning(
                "proactive: apply_preset(%r) failed: %s", name, result.get("error")
            )
        else:
            logger.info("proactive: applied mode %r for %s", name, transition.key)

    def _preset_for(self, transition: _Transition) -> str:
        if transition.preset_role == "day":
            return self._config.day_preset
        if transition.preset_role == "night":
            return self._config.night_preset
        return ""

    # ---- guards + matching -----------------------------------------

    def _match_transition(
        self, old: PresenceState, new: PresenceState
    ) -> _Transition | None:
        for t in _ALL_TRANSITIONS:
            if t.key in self._config.enabled_transitions and t.src == old and t.dst == new:
                return t
        return None

    def _skip_reason(self, transition: _Transition) -> str | None:
        """Mirror of the heartbeat's layered suppression (principle #1).

        Copied rather than shared: the heartbeat's guards bind to its own
        instance state, and refactoring them would churn passing tests.
        ``transition.exempt_quiet_hours`` lets the night "おやすみ" line fire
        inside the quiet window (it is itself the night greeting).
        """
        gw = self._gateway
        if not gw.esp32.device_connected:
            return "no device connected"
        if getattr(gw, "voice_turn_active", False):
            return "voice turn active"
        if self._multiturn_suppresses():
            return "multiturn continuation"
        if gw.esp32.tts_lock.locked():
            return "audio pipeline busy"
        if is_recording():
            return "recording active"
        if not transition.exempt_quiet_hours and is_quiet(
            self._now(), self._config.quiet
        ):
            return "quiet hours"
        # Occupancy gate: defence in depth. A *_active transition lands on
        # ACTIVE so the room is occupied, but a stale gate would suppress
        # rather than mis-fire, which is the safe direction.
        presence = getattr(gw, "_presence", None)
        if presence is not None and not presence.allows_heartbeat():
            return "room empty"
        last = getattr(gw, "last_human_interaction_monotonic", None)
        if (
            last is not None
            and self._monotonic() - last < self._config.cooldown_min * 60.0
        ):
            return "recent interaction"
        if self._spoken_today() >= self._config.max_per_day:
            return "daily cap"
        return None

    def _multiturn_suppresses(self) -> bool:
        """True while a multi-turn continuation gap is open and fresh.

        Same check the heartbeat uses: between an auto-continued turn and
        the user's answer ``voice_turn_active`` is briefly False, so a
        separate flag covers that gap. Self-expiring on the session
        timeout so a lost answer can never wedge proactive speech off.
        """
        gw = self._gateway
        if not getattr(gw, "multiturn_active", False):
            return False
        session = getattr(gw, "multiturn", None)
        if session is None:
            return False
        from . import multiturn

        return not session.is_gap_stale(
            self._monotonic(), multiturn.session_timeout_s()
        )

    def _refire_suppressed(self, key: str) -> bool:
        last = self._last_fire_mono.get(key)
        if last is None:
            return False
        return (self._monotonic() - last) < self._config.refire_min * 60.0

    def _situation(self, transition: _Transition) -> str:
        now = self._now().strftime("%H:%M")
        return (
            f"（状況: {transition.situation}。今は {now} です）"
            "短く自然に、一言だけ声をかけてください。"
        )

    # ---- speech + state --------------------------------------------

    async def _perform_speak(self, text: str, key: str) -> None:
        # Lazy import keeps capture-only deployments free of the tts extras
        # (same pattern as heartbeat._perform_speak / hermes_bridge).
        from .tts.orchestrator import synthesize_and_send

        logger.info("proactive: speak %r (%s)", text, key)
        # NOTE: the refire window was already stamped in on_state_change (it
        # covers the mode switch too). We deliberately do *not* call
        # note_human_interaction here — a proactive line is not a user action,
        # so it must not reset its own cooldown clock.
        await self._set_face("happy")
        try:
            await synthesize_and_send({"text": text}, gateway=self._gateway)
        finally:
            await self._set_face("idle")
        self._bump_daily_count()

    async def _set_face(self, face: str) -> None:
        _result, error = await self._gateway.esp32.call_tool(
            "self.display.set_avatar", {"face": face}
        )
        if error:
            logger.warning("proactive: set_avatar failed: %s", error)

    def _spoken_today(self) -> int:
        if self._state.get("speak_count_date") != self._today().isoformat():
            return 0
        try:
            return int(self._state.get("speak_count", 0))
        except (TypeError, ValueError):
            return 0

    def _bump_daily_count(self) -> None:
        today = self._today().isoformat()
        if self._state.get("speak_count_date") != today:
            self._state["speak_count_date"] = today
            self._state["speak_count"] = 0
        self._state["speak_count"] = self._spoken_today() + 1
        self._save_state()

    def _load_state(self) -> dict[str, Any]:
        path = self._config.state_path
        try:
            data = json.loads(path.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning("proactive: unreadable state file %s (%s)", path, exc)
            return {}

    def _save_state(self) -> None:
        # Atomic write (write-temp + os.replace), same flavour as the
        # heartbeat/control state files: a crash mid-write must not
        # truncate the day's speak-count into garbage.
        path = self._config.state_path
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
            logger.warning("proactive: cannot write state file %s (%s)", path, exc)

    # ---- test seams (mirror heartbeat) -----------------------------

    def _now(self) -> _dt.time:
        return _dt.datetime.now().time()

    def _today(self) -> _dt.date:
        return _dt.date.today()

    def _monotonic(self) -> float:
        return time.monotonic()
