"""Multi-turn voice conversation continuation (yorishiro fork).

After a voice turn whose Hermes reply ends with a question mark, the
gateway re-opens listening automatically so the user can answer without
tapping again. The firmware's VAD silence auto-stop (``PollTouchpad`` in
``firmware/main/boards/stackchan/stackchan.cc``) closes each follow-up
listen exactly as it closes a tapped one — entering ``kDeviceStateListening``
resets the VAD warmup/auto-stop state regardless of *what* triggered the
listen — so the loop needs no firmware change.

Design constraints (CLAUDE.md design principle #1 — never interrupt the
couple's conversation): continuation is *bounded*. It only fires right
after Hermes ends a turn with a question, for a finite number of
consecutive turns, and stops the moment the user stays silent (an empty
transcript) or the session goes stale. There is **no always-on VAD** —
every conversation still starts from an explicit tap / back-touch.

This module is pure logic plus a small per-conversation state object.
The :class:`~stackchan_mcp.gateway.Gateway` owns the state instance so it
survives across the independent ``/voice_turn`` HTTP POSTs that make up
one spoken conversation.

Environment variables (all opt-in / tunable):

- ``STACKCHAN_MULTITURN`` — master gate. Unset/false = the feature is
  off and every turn ends after one round-trip, exactly as before.
- ``MAX_MULTITURN_TURNS`` — ceiling on consecutive auto-continuations in
  one conversation (default 4).
- ``MULTITURN_SESSION_TIMEOUT_S`` — a continuation gap older than this is
  treated as abandoned: the turn counter resets and the heartbeat is no
  longer suppressed (default 60).
- ``MULTITURN_TTS_GUARD_MS`` — fixed delay before re-opening listening,
  covering the firmware decode-queue drain after ``synthesize_and_send``
  returns (frames are paced at real time, so only the ~0.8 s queue tail
  remains). Default 1000; tune down on hardware once self-pickup is
  ruled out (AEC is off, so re-opening too early can let the device hear
  its own tail).
- ``HERMES_SESSION_WINDOW_S`` — Phase 2 context retention. Seconds of
  inactivity after which the next turn starts a *fresh* Hermes
  conversation (a new ``X-Hermes-Session-Id``). Within the window the
  turns of one conversation reuse the same id so Hermes keeps context;
  past it a new tap rotates to a new id so conversations no longer pile
  into one ever-growing session (the day-spanning accumulation the fixed
  ``HERMES_SESSION_ID`` caused). Default 180; ``0`` disables rotation and
  restores the fixed-id behaviour exactly.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from . import local_llm

#: Reply suffixes that invite a follow-up answer (ASCII + full-width).
_CONTINUE_SUFFIXES = ("?", "？")

DEFAULT_MAX_TURNS = 4
DEFAULT_SESSION_TIMEOUT_S = 60
DEFAULT_TTS_GUARD_MS = 1000
DEFAULT_SESSION_WINDOW_S = 180


def _env_positive_int(name: str, default: int) -> int:
    """Parse a positive int env var, falling back to ``default``."""
    try:
        value = int(os.getenv(name, ""))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def is_enabled() -> bool:
    """True when multi-turn continuation is opted in via the env gate.

    Phase 1 (MVP) gates on the env var alone; Phase 3 will OR this with a
    persisted dashboard toggle.
    """
    return os.getenv("STACKCHAN_MULTITURN", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def max_turns() -> int:
    """Ceiling on consecutive auto-continuations in one conversation."""
    return _env_positive_int("MAX_MULTITURN_TURNS", DEFAULT_MAX_TURNS)


def session_timeout_s() -> float:
    """Seconds of inactivity after which a continuation gap is abandoned."""
    return float(_env_positive_int("MULTITURN_SESSION_TIMEOUT_S", DEFAULT_SESSION_TIMEOUT_S))


def tts_guard_ms() -> int:
    """Fixed delay (ms) before re-opening listening after a reply plays.

    Zero is allowed (no guard); negative/invalid falls back to the default.
    """
    raw = os.getenv("MULTITURN_TTS_GUARD_MS", "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TTS_GUARD_MS
    return value if value >= 0 else DEFAULT_TTS_GUARD_MS


def session_window_s() -> float:
    """Seconds of inactivity after which the next turn rotates to a fresh
    Hermes conversation id (Phase 2 context retention).

    ``0`` is allowed and disables rotation — every turn reuses the fixed
    ``HERMES_SESSION_ID``, exactly as before. Negative/invalid falls back
    to the default.
    """
    raw = os.getenv("HERMES_SESSION_WINDOW_S", "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return float(DEFAULT_SESSION_WINDOW_S)
    return float(value) if value >= 0 else float(DEFAULT_SESSION_WINDOW_S)


def new_session_id(base: str) -> str:
    """Mint a fresh per-conversation Hermes session id under ``base``.

    The configured ``HERMES_SESSION_ID`` is kept as a stable namespace
    prefix so conversations stay identifiable in Hermes' logs while no
    longer accumulating into one persistent session.
    """
    return f"{base}-{uuid.uuid4().hex[:8]}"


def reply_invites_continuation(reply: str) -> bool:
    """True when ``reply`` ends with a question mark (ASCII or full-width)."""
    return reply.rstrip().endswith(_CONTINUE_SUFFIXES)


@dataclass
class MultiturnSession:
    """Per-conversation continuation state held on the Gateway.

    One spoken conversation can span several independent ``/voice_turn``
    POSTs; this object carries the consecutive-continuation counter and a
    monotonic activity stamp across them.

    ``turn_count`` counts only auto-continuations that actually fired — a
    fresh tapped turn starts at 0. ``last_activity`` is a ``time.monotonic``
    stamp advanced at the start of every turn (and whenever a continuation
    fires), used to expire a stale gap, to stop the heartbeat from being
    suppressed forever if the user's answer never arrives, and to decide
    whether the next turn shares the in-flight Hermes conversation id.

    ``session_id`` is the Phase 2 per-conversation Hermes context id
    (``X-Hermes-Session-Id``). It survives a :meth:`reset` (which only
    clears the multiturn counter) and rotates purely on the inactivity
    window via :meth:`conversation_id`.
    """

    turn_count: int = 0
    last_activity: float = 0.0
    session_id: str = ""

    def reset(self) -> None:
        """End the conversation: clear the continuation counter.

        The Hermes ``session_id`` is intentionally left intact — a brief
        silence (empty transcript) or a ceiling hit ends the *multiturn*
        loop, but a follow-up tap within the context window is still the
        same conversation. Session-id rotation is governed solely by the
        inactivity window in :meth:`conversation_id`.
        """
        self.turn_count = 0

    def note_continuation(self, now: float) -> None:
        """Record that one more auto-continuation just fired."""
        self.turn_count += 1
        self.last_activity = now

    def is_gap_stale(self, now: float, timeout_s: float) -> bool:
        """True when an open continuation gap has outlived ``timeout_s``."""
        return self.turn_count > 0 and (now - self.last_activity) > timeout_s

    def conversation_id(
        self, *, now: float, window_s: float, mint: Callable[[], str]
    ) -> str:
        """Return the Hermes conversation id for a turn starting at ``now``.

        Rotates to a fresh ``mint()``-ed id when no conversation is open
        yet or the gap since the last turn exceeds ``window_s``; within
        the window the in-flight id is reused so the turns of one
        conversation share Hermes context. ``window_s == 0`` disables
        rotation and leaves the id untouched (the caller falls back to the
        fixed base id).

        Reads ``last_activity`` *before* the caller advances it for this
        turn, so call it at turn entry prior to stamping the new activity.
        """
        if window_s > 0 and (
            not self.session_id or (now - self.last_activity) > window_s
        ):
            self.session_id = mint()
        return self.session_id


def should_continue(
    *,
    enabled: bool,
    route: str,
    reply: str,
    turn_count: int,
    max_turns: int,
    device_connected: bool,
    muted: bool,
    recording: bool,
) -> bool:
    """Decide whether to auto-reopen listening after a completed turn.

    Pure function — all inputs are explicit so the policy can be unit
    tested without a gateway, env, or device. Every condition must hold:

    - ``enabled``: the feature toggle is on
    - ``route == hermes``: local-LLM turns are excluded (the local model
      is weak at judging whether to keep talking)
    - the reply ends with a question mark
    - ``turn_count < max_turns``: under the per-conversation ceiling
    - the device is connected, not muted, and not already recording
    """
    return (
        enabled
        and route == local_llm.ROUTE_HERMES
        and reply_invites_continuation(reply)
        and turn_count < max_turns
        and device_connected
        and not muted
        and not recording
    )
