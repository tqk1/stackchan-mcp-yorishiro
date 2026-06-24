"""Tests for the proactive speaker (Hermes 自発判断層).

The speaker is driven through :meth:`ProactiveSpeaker.on_state_change`
with patched clocks and stubbed Hermes/TTS so no real HTTP, ESP32 or
asyncio sleep is involved. The central regression guarded here is that a
startup ``UNKNOWN→ACTIVE`` never speaks, and that every heartbeat-style
suppression (voice turn, audio lock, quiet hours, cooldown, daily cap,
recording, multi-turn gap) plus the per-transition refire cooldown holds.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import pytest

from stackchan_mcp import proactive
from stackchan_mcp.presence import PresenceState
from stackchan_mcp.proactive import ProactiveConfig, ProactiveSpeaker


class FakeESP32:
    def __init__(self, connected: bool = True) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.device_connected = connected
        self.tts_lock = asyncio.Lock()

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"ok": True}, None


class FakeGateway:
    def __init__(self, connected: bool = True) -> None:
        self.esp32 = FakeESP32(connected)
        self.voice_turn_active = False
        self.last_human_interaction_monotonic = None
        self._presence = None
        self.multiturn_active = False
        self.multiturn = None


class FakeSession:
    """Minimal multi-turn session: a fixed gap-staleness verdict."""

    def __init__(self, *, stale: bool) -> None:
        self._stale = stale

    def is_gap_stale(self, now: float, timeout: float) -> bool:
        return self._stale


@pytest.fixture(autouse=True)
def _proactive_state_path(monkeypatch, tmp_path):
    # Keep from_env's default path off the real home dir.
    monkeypatch.setenv(
        "STACKCHAN_PROACTIVE_STATE", str(tmp_path / "proactive_state.json")
    )


def make_speaker(gateway=None, *, tmp_path=None, **config_kw) -> ProactiveSpeaker:
    config_kw.setdefault("quiet", None)  # quiet off unless a test sets it
    if tmp_path is not None:
        config_kw.setdefault("state_path", tmp_path / "proactive_state.json")
    return ProactiveSpeaker(gateway or FakeGateway(), config=ProactiveConfig(**config_kw))


def install_stubs(
    monkeypatch,
    *,
    reply: str = "おかえりなさい",
    enabled: bool = True,
    ask_raises: bool = False,
    recording: bool = False,
    apply_ok: bool = True,
) -> dict:
    """Patch the lazily-imported control / Hermes / TTS seams.

    Returns a recorder dict: ``asked`` (system_prompt + text per call),
    ``spoken`` (text passed to TTS), ``modes`` (preset names applied),
    ``delays`` (mode-switch pause durations) and ``seq`` (interleaved
    "speak"/"delay"/"mode:<name>" markers so a test can assert the mute-safe
    ordering and the beat between the greeting and the mode switch).
    """
    rec: dict = {"asked": [], "spoken": [], "modes": [], "delays": [], "seq": []}

    monkeypatch.setattr("stackchan_mcp.control.proactive_enabled", lambda: enabled)
    monkeypatch.setattr(proactive, "is_recording", lambda: recording)

    async def fake_sleep(secs):
        # The mode-switch beat — recorded, never actually slept (keeps the
        # suite fast and lets a test assert the pause landed between the two).
        rec["seq"].append("delay")
        rec["delays"].append(secs)

    monkeypatch.setattr(proactive.asyncio, "sleep", fake_sleep)

    async def fake_ask(text, **kwargs):
        rec["asked"].append({"text": text, "system_prompt": kwargs.get("system_prompt")})
        if ask_raises:
            raise RuntimeError("hermes down")
        return reply

    async def fake_synth(arguments, **kwargs):
        rec["spoken"].append(arguments["text"])
        rec["seq"].append("speak")
        return {"ok": True}

    async def fake_apply(gateway, name):
        rec["modes"].append(name)
        rec["seq"].append(f"mode:{name}")
        if apply_ok:
            return {"ok": True, "preset": name}
        return {"ok": False, "error": f"preset '{name}' not found"}

    monkeypatch.setattr("stackchan_mcp.hermes_bridge.ask_hermes", fake_ask)
    monkeypatch.setattr(
        "stackchan_mcp.tts.orchestrator.synthesize_and_send", fake_synth
    )
    monkeypatch.setattr("stackchan_mcp.control.apply_preset", fake_apply)
    return rec


def faces(gw: FakeGateway) -> list[str]:
    return [a["face"] for n, a in gw.esp32.calls if n == "self.display.set_avatar"]


# ---- from_env --------------------------------------------------------


def test_from_env_disabled_without_master_switch(monkeypatch):
    monkeypatch.delenv("STACKCHAN_PROACTIVE", raising=False)
    assert ProactiveSpeaker.from_env(FakeGateway()) is None


def test_from_env_builds_when_enabled(monkeypatch):
    monkeypatch.setenv("STACKCHAN_PROACTIVE", "1")
    speaker = ProactiveSpeaker.from_env(FakeGateway())
    assert speaker is not None
    assert speaker._config.enabled_transitions == {
        "absent_active",
        "quiet_active",
        "active_quiet",
    }
    assert speaker._config.max_per_day == 4
    # Default mode presets match the dashboard cards the user saves.
    assert speaker._config.day_preset == "つうじょう"
    assert speaker._config.night_preset == "おやすみ"
    assert speaker._config.mode_switch_delay_s == proactive.DEFAULT_MODE_DELAY_S


def test_from_env_parses_transitions(monkeypatch):
    monkeypatch.setenv("STACKCHAN_PROACTIVE", "on")
    monkeypatch.setenv("STACKCHAN_PROACTIVE_TRANSITIONS", "quiet_active,bogus")
    speaker = ProactiveSpeaker.from_env(FakeGateway())
    assert speaker is not None
    assert speaker._config.enabled_transitions == {"quiet_active"}


# ---- firing ----------------------------------------------------------


@pytest.mark.asyncio
async def test_absent_active_fires(monkeypatch, tmp_path):
    gw = FakeGateway()
    speaker = make_speaker(gw, tmp_path=tmp_path)
    rec = install_stubs(monkeypatch, reply="おかえり")
    await speaker.on_state_change(PresenceState.ABSENT, PresenceState.ACTIVE)
    assert rec["spoken"] == ["おかえり"]
    # The proactive system prompt (not the chat prompt) framed the ask.
    assert rec["asked"][0]["system_prompt"] == proactive.PROACTIVE_SYSTEM_PROMPT
    assert faces(gw) == ["happy", "idle"]
    assert speaker._spoken_today() == 1


@pytest.mark.asyncio
async def test_quiet_active_fires(monkeypatch, tmp_path):
    gw = FakeGateway()
    speaker = make_speaker(gw, tmp_path=tmp_path)
    rec = install_stubs(monkeypatch, reply="おはよう")
    await speaker.on_state_change(PresenceState.QUIET, PresenceState.ACTIVE)
    assert rec["spoken"] == ["おはよう"]


@pytest.mark.asyncio
async def test_unknown_active_never_fires(monkeypatch, tmp_path):
    # The startup / reconnect regression: a first reading or a device
    # reconnect lands as UNKNOWN→ACTIVE and must stay silent.
    gw = FakeGateway()
    speaker = make_speaker(gw, tmp_path=tmp_path)
    rec = install_stubs(monkeypatch)
    await speaker.on_state_change(PresenceState.UNKNOWN, PresenceState.ACTIVE)
    assert rec["asked"] == []
    assert rec["spoken"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "old,new",
    [
        (PresenceState.ACTIVE, PresenceState.ABSENT),
        (PresenceState.ABSENT, PresenceState.QUIET),  # not in default set
        (PresenceState.QUIET, PresenceState.ABSENT),
    ],
)
async def test_non_target_transitions_do_not_fire(monkeypatch, tmp_path, old, new):
    gw = FakeGateway()
    speaker = make_speaker(gw, tmp_path=tmp_path)
    rec = install_stubs(monkeypatch)
    await speaker.on_state_change(old, new)
    assert rec["asked"] == []
    assert rec["spoken"] == []
    assert rec["modes"] == []


@pytest.mark.asyncio
async def test_disabled_toggle_skips(monkeypatch, tmp_path):
    gw = FakeGateway()
    speaker = make_speaker(gw, tmp_path=tmp_path)
    rec = install_stubs(monkeypatch, enabled=False)
    await speaker.on_state_change(PresenceState.ABSENT, PresenceState.ACTIVE)
    assert rec["asked"] == []
    assert rec["spoken"] == []


@pytest.mark.asyncio
async def test_hermes_failure_stays_silent(monkeypatch, tmp_path):
    gw = FakeGateway()
    speaker = make_speaker(gw, tmp_path=tmp_path)
    rec = install_stubs(monkeypatch, ask_raises=True)
    # Must not raise into the monitor's fire-and-forget task.
    await speaker.on_state_change(PresenceState.ABSENT, PresenceState.ACTIVE)
    assert rec["asked"]  # it tried
    assert rec["spoken"] == []  # but nothing was played
    assert speaker._spoken_today() == 0


# ---- mode auto-switch + active_quiet ---------------------------------


@pytest.mark.asyncio
async def test_active_quiet_fires_in_quiet_hours_then_mutes(monkeypatch, tmp_path):
    # The night transition lands at the start of the quiet window. It is
    # exempt from the quiet-hours guard (おやすみ *is* the night greeting),
    # and must speak *before* the muting preset is applied.
    from stackchan_mcp.heartbeat import parse_quiet_hours

    gw = FakeGateway()
    speaker = make_speaker(
        gw, tmp_path=tmp_path, quiet=parse_quiet_hours("22:00-06:30")
    )
    rec = install_stubs(monkeypatch, reply="おやすみ")
    monkeypatch.setattr(speaker, "_now", lambda: dt.time(23, 30))
    await speaker.on_state_change(PresenceState.ACTIVE, PresenceState.QUIET)
    assert rec["spoken"] == ["おやすみ"]
    assert rec["modes"] == ["おやすみ"]
    # Speak, a natural beat, *then* apply the muting preset.
    assert rec["seq"] == ["speak", "delay", "mode:おやすみ"]
    assert rec["delays"] == [proactive.DEFAULT_MODE_DELAY_S]
    assert faces(gw) == ["happy", "idle"]


@pytest.mark.asyncio
@pytest.mark.parametrize("old", [PresenceState.ABSENT, PresenceState.QUIET])
async def test_day_transition_applies_mode_before_speaking(monkeypatch, tmp_path, old):
    # Becoming active (return home / morning wake) restores the un-muting
    # day mode *first*, then greets, so the line is never swallowed by a
    # lingering overnight mute.
    gw = FakeGateway()
    speaker = make_speaker(gw, tmp_path=tmp_path)
    rec = install_stubs(monkeypatch, reply="おはよう")
    await speaker.on_state_change(old, PresenceState.ACTIVE)
    assert rec["modes"] == ["つうじょう"]
    assert rec["spoken"] == ["おはよう"]
    # Un-mute / brighten, a natural beat, then greet.
    assert rec["seq"] == ["mode:つうじょう", "delay", "speak"]


@pytest.mark.asyncio
async def test_active_quiet_blocked_by_voice_turn_keeps_mode(monkeypatch, tmp_path):
    # The mode switch shares the conversation guards: never mute / dim while
    # the household is mid-turn (design principle #1) — both halves skip.
    from stackchan_mcp.heartbeat import parse_quiet_hours

    gw = FakeGateway()
    gw.voice_turn_active = True
    speaker = make_speaker(
        gw, tmp_path=tmp_path, quiet=parse_quiet_hours("22:00-06:30")
    )
    rec = install_stubs(monkeypatch)
    monkeypatch.setattr(speaker, "_now", lambda: dt.time(23, 30))
    await speaker.on_state_change(PresenceState.ACTIVE, PresenceState.QUIET)
    assert rec["spoken"] == []
    assert rec["modes"] == []


@pytest.mark.asyncio
async def test_mode_apply_failure_does_not_block_greeting(monkeypatch, tmp_path):
    # A missing / failing preset is best-effort: the greeting still plays.
    gw = FakeGateway()
    speaker = make_speaker(gw, tmp_path=tmp_path)
    rec = install_stubs(monkeypatch, reply="おかえり", apply_ok=False)
    await speaker.on_state_change(PresenceState.ABSENT, PresenceState.ACTIVE)
    assert rec["modes"] == ["つうじょう"]  # it tried
    assert rec["spoken"] == ["おかえり"]  # greeting still plays


@pytest.mark.asyncio
async def test_hermes_failure_still_applies_night_mode(monkeypatch, tmp_path):
    # Night mode must switch even if Hermes is down — the greeting is the
    # best-effort half, the mode switch is the reliable one.
    from stackchan_mcp.heartbeat import parse_quiet_hours

    gw = FakeGateway()
    speaker = make_speaker(
        gw, tmp_path=tmp_path, quiet=parse_quiet_hours("22:00-06:30")
    )
    rec = install_stubs(monkeypatch, ask_raises=True)
    monkeypatch.setattr(speaker, "_now", lambda: dt.time(23, 30))
    await speaker.on_state_change(PresenceState.ACTIVE, PresenceState.QUIET)
    assert rec["spoken"] == []  # Hermes down -> stays silent
    assert rec["modes"] == ["おやすみ"]  # room still goes to night mode


@pytest.mark.asyncio
async def test_empty_preset_name_disables_switch(monkeypatch, tmp_path):
    # An empty configured preset name means "greeting only" for that side.
    gw = FakeGateway()
    speaker = make_speaker(gw, tmp_path=tmp_path, day_preset="")
    rec = install_stubs(monkeypatch, reply="おかえり")
    await speaker.on_state_change(PresenceState.ABSENT, PresenceState.ACTIVE)
    assert rec["modes"] == []  # day-mode switch disabled
    assert rec["spoken"] == ["おかえり"]  # greeting still happens
    assert rec["seq"] == ["speak"]  # no mode -> no beat either


@pytest.mark.asyncio
async def test_mode_switch_delay_is_configurable(monkeypatch, tmp_path):
    gw = FakeGateway()
    speaker = make_speaker(gw, tmp_path=tmp_path, mode_switch_delay_s=2.0)
    rec = install_stubs(monkeypatch, reply="おかえり")
    await speaker.on_state_change(PresenceState.ABSENT, PresenceState.ACTIVE)
    assert rec["delays"] == [2.0]
    assert rec["seq"] == ["mode:つうじょう", "delay", "speak"]


@pytest.mark.asyncio
async def test_mode_switch_delay_zero_is_immediate(monkeypatch, tmp_path):
    gw = FakeGateway()
    speaker = make_speaker(gw, tmp_path=tmp_path, mode_switch_delay_s=0.0)
    rec = install_stubs(monkeypatch, reply="おかえり")
    await speaker.on_state_change(PresenceState.ABSENT, PresenceState.ACTIVE)
    assert rec["delays"] == []  # no beat
    assert rec["seq"] == ["mode:つうじょう", "speak"]


# ---- guards (design principle #1) ------------------------------------


@pytest.mark.asyncio
async def test_guard_device_disconnected(monkeypatch, tmp_path):
    gw = FakeGateway(connected=False)
    speaker = make_speaker(gw, tmp_path=tmp_path)
    rec = install_stubs(monkeypatch)
    await speaker.on_state_change(PresenceState.ABSENT, PresenceState.ACTIVE)
    assert rec["spoken"] == []


@pytest.mark.asyncio
async def test_guard_voice_turn_active(monkeypatch, tmp_path):
    gw = FakeGateway()
    gw.voice_turn_active = True
    speaker = make_speaker(gw, tmp_path=tmp_path)
    rec = install_stubs(monkeypatch)
    await speaker.on_state_change(PresenceState.ABSENT, PresenceState.ACTIVE)
    assert rec["spoken"] == []


@pytest.mark.asyncio
async def test_guard_audio_pipeline_busy(monkeypatch, tmp_path):
    gw = FakeGateway()
    speaker = make_speaker(gw, tmp_path=tmp_path)
    rec = install_stubs(monkeypatch)
    async with gw.esp32.tts_lock:
        await speaker.on_state_change(PresenceState.ABSENT, PresenceState.ACTIVE)
    assert rec["spoken"] == []


@pytest.mark.asyncio
async def test_guard_recording(monkeypatch, tmp_path):
    gw = FakeGateway()
    speaker = make_speaker(gw, tmp_path=tmp_path)
    rec = install_stubs(monkeypatch, recording=True)
    await speaker.on_state_change(PresenceState.ABSENT, PresenceState.ACTIVE)
    assert rec["spoken"] == []


@pytest.mark.asyncio
async def test_guard_quiet_hours(monkeypatch, tmp_path):
    from stackchan_mcp.heartbeat import parse_quiet_hours

    gw = FakeGateway()
    speaker = make_speaker(
        gw, tmp_path=tmp_path, quiet=parse_quiet_hours("22:00-06:30")
    )
    rec = install_stubs(monkeypatch)
    monkeypatch.setattr(speaker, "_now", lambda: dt.time(23, 30))
    await speaker.on_state_change(PresenceState.ABSENT, PresenceState.ACTIVE)
    assert rec["spoken"] == []
    # Outside the window it speaks.
    monkeypatch.setattr(speaker, "_now", lambda: dt.time(12, 0))
    await speaker.on_state_change(PresenceState.ABSENT, PresenceState.ACTIVE)
    assert rec["spoken"] == ["おかえりなさい"]


@pytest.mark.asyncio
async def test_guard_multiturn_continuation(monkeypatch, tmp_path):
    gw = FakeGateway()
    gw.multiturn_active = True
    gw.multiturn = FakeSession(stale=False)  # a fresh gap suppresses
    speaker = make_speaker(gw, tmp_path=tmp_path)
    rec = install_stubs(monkeypatch)
    await speaker.on_state_change(PresenceState.ABSENT, PresenceState.ACTIVE)
    assert rec["spoken"] == []
    # A stale gap no longer suppresses.
    gw.multiturn = FakeSession(stale=True)
    await speaker.on_state_change(PresenceState.ABSENT, PresenceState.ACTIVE)
    assert rec["spoken"] == ["おかえりなさい"]


@pytest.mark.asyncio
async def test_guard_recent_interaction_cooldown(monkeypatch, tmp_path):
    gw = FakeGateway()
    speaker = make_speaker(gw, tmp_path=tmp_path, cooldown_min=20.0)
    rec = install_stubs(monkeypatch)
    monkeypatch.setattr(speaker, "_monotonic", lambda: 10_000.0)
    # 5 minutes ago -> still in cooldown.
    gw.last_human_interaction_monotonic = 10_000.0 - 5 * 60
    await speaker.on_state_change(PresenceState.ABSENT, PresenceState.ACTIVE)
    assert rec["spoken"] == []
    # 25 minutes ago -> past cooldown.
    gw.last_human_interaction_monotonic = 10_000.0 - 25 * 60
    await speaker.on_state_change(PresenceState.ABSENT, PresenceState.ACTIVE)
    assert rec["spoken"] == ["おかえりなさい"]


@pytest.mark.asyncio
async def test_guard_daily_cap(monkeypatch, tmp_path):
    gw = FakeGateway()
    speaker = make_speaker(gw, tmp_path=tmp_path, max_per_day=1, refire_min=0.0)
    rec = install_stubs(monkeypatch)
    today = dt.date(2026, 6, 22)
    monkeypatch.setattr(speaker, "_today", lambda: today)
    await speaker.on_state_change(PresenceState.ABSENT, PresenceState.ACTIVE)
    assert rec["spoken"] == ["おかえりなさい"]  # 1st: ok
    await speaker.on_state_change(PresenceState.ABSENT, PresenceState.ACTIVE)
    assert rec["spoken"] == ["おかえりなさい"]  # 2nd: capped
    # Next day rolls the counter over.
    monkeypatch.setattr(speaker, "_today", lambda: dt.date(2026, 6, 23))
    await speaker.on_state_change(PresenceState.ABSENT, PresenceState.ACTIVE)
    assert rec["spoken"] == ["おかえりなさい", "おかえりなさい"]


# ---- refire cooldown (anti-chatter) ----------------------------------


@pytest.mark.asyncio
async def test_refire_cooldown(monkeypatch, tmp_path):
    gw = FakeGateway()
    speaker = make_speaker(gw, tmp_path=tmp_path, refire_min=10.0, max_per_day=99)
    rec = install_stubs(monkeypatch)
    clock = {"t": 1_000.0}
    monkeypatch.setattr(speaker, "_monotonic", lambda: clock["t"])
    await speaker.on_state_change(PresenceState.ABSENT, PresenceState.ACTIVE)
    assert len(rec["spoken"]) == 1
    # 5 min later: same transition kind is still in its refire window.
    clock["t"] = 1_000.0 + 5 * 60
    await speaker.on_state_change(PresenceState.ABSENT, PresenceState.ACTIVE)
    assert len(rec["spoken"]) == 1
    # 11 min after the first: window elapsed, fires again.
    clock["t"] = 1_000.0 + 11 * 60
    await speaker.on_state_change(PresenceState.ABSENT, PresenceState.ACTIVE)
    assert len(rec["spoken"]) == 2


@pytest.mark.asyncio
async def test_refire_is_per_transition_kind(monkeypatch, tmp_path):
    # A morning wake right after a return must not be eaten by the
    # return's refire window — the cooldown is keyed per transition.
    gw = FakeGateway()
    speaker = make_speaker(gw, tmp_path=tmp_path, refire_min=10.0, max_per_day=99)
    rec = install_stubs(monkeypatch)
    monkeypatch.setattr(speaker, "_monotonic", lambda: 500.0)
    await speaker.on_state_change(PresenceState.ABSENT, PresenceState.ACTIVE)
    await speaker.on_state_change(PresenceState.QUIET, PresenceState.ACTIVE)
    assert len(rec["spoken"]) == 2


# ---- state persistence -----------------------------------------------


def test_save_state_is_atomic(monkeypatch, tmp_path):
    state_path = tmp_path / "proactive_state.json"
    speaker = make_speaker(state_path=state_path)
    speaker._state = {"speak_count_date": "2026-06-22", "speak_count": 2}

    replaced: list[tuple[str, str]] = []
    real_replace = proactive.os.replace

    def spy_replace(src, dst):
        replaced.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(proactive.os, "replace", spy_replace)
    speaker._save_state()

    assert replaced and replaced[-1][1] == str(state_path)
    assert json.loads(state_path.read_text("utf-8")) == speaker._state
    assert list(tmp_path.glob("*.tmp")) == []


def test_daily_count_persists_across_restart(monkeypatch, tmp_path):
    state_path = tmp_path / "proactive_state.json"
    speaker = make_speaker(state_path=state_path)
    today = dt.date(2026, 6, 22)
    monkeypatch.setattr(speaker, "_today", lambda: today)
    speaker._bump_daily_count()
    assert speaker._spoken_today() == 1
    # A fresh speaker reading the same file sees the same count.
    fresh = make_speaker(state_path=state_path)
    monkeypatch.setattr(fresh, "_today", lambda: today)
    assert fresh._spoken_today() == 1
