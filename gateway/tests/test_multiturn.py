"""Tests for the multi-turn continuation policy (pure logic)."""

import pytest

from stackchan_mcp import multiturn
from stackchan_mcp.multiturn import MultiturnSession, should_continue


# ---- reply_invites_continuation ------------------------------------------


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("元気にしてた？", True),
        ("How are you?", True),
        ("今日はどうだった？  ", True),  # trailing whitespace ignored
        ("そうなんだ。", False),
        ("いいね！", False),
        ("", False),
        ("?", True),
        ("？", True),
    ],
)
def test_reply_invites_continuation(reply, expected):
    assert multiturn.reply_invites_continuation(reply) is expected


# ---- config getters (env parsing) ----------------------------------------


def test_is_enabled_defaults_off(monkeypatch):
    monkeypatch.delenv("STACKCHAN_MULTITURN", raising=False)
    assert multiturn.is_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " On "])
def test_is_enabled_truthy(monkeypatch, value):
    monkeypatch.setenv("STACKCHAN_MULTITURN", value)
    assert multiturn.is_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_is_enabled_falsey(monkeypatch, value):
    monkeypatch.setenv("STACKCHAN_MULTITURN", value)
    assert multiturn.is_enabled() is False


def test_max_turns_default_and_override(monkeypatch):
    monkeypatch.delenv("MAX_MULTITURN_TURNS", raising=False)
    assert multiturn.max_turns() == multiturn.DEFAULT_MAX_TURNS
    monkeypatch.setenv("MAX_MULTITURN_TURNS", "7")
    assert multiturn.max_turns() == 7
    # Garbage / non-positive falls back to the default.
    monkeypatch.setenv("MAX_MULTITURN_TURNS", "nonsense")
    assert multiturn.max_turns() == multiturn.DEFAULT_MAX_TURNS
    monkeypatch.setenv("MAX_MULTITURN_TURNS", "0")
    assert multiturn.max_turns() == multiturn.DEFAULT_MAX_TURNS


def test_tts_guard_ms_allows_zero(monkeypatch):
    monkeypatch.setenv("MULTITURN_TTS_GUARD_MS", "0")
    assert multiturn.tts_guard_ms() == 0
    monkeypatch.setenv("MULTITURN_TTS_GUARD_MS", "350")
    assert multiturn.tts_guard_ms() == 350
    # Negative / garbage falls back to the default.
    monkeypatch.setenv("MULTITURN_TTS_GUARD_MS", "-5")
    assert multiturn.tts_guard_ms() == multiturn.DEFAULT_TTS_GUARD_MS
    monkeypatch.setenv("MULTITURN_TTS_GUARD_MS", "x")
    assert multiturn.tts_guard_ms() == multiturn.DEFAULT_TTS_GUARD_MS


def test_session_timeout_default(monkeypatch):
    monkeypatch.delenv("MULTITURN_SESSION_TIMEOUT_S", raising=False)
    assert multiturn.session_timeout_s() == float(multiturn.DEFAULT_SESSION_TIMEOUT_S)


# ---- Phase 2 session-id window (env + minting) ---------------------------


def test_session_window_default(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_WINDOW_S", raising=False)
    assert multiturn.session_window_s() == float(multiturn.DEFAULT_SESSION_WINDOW_S)


def test_session_window_override_and_zero(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_WINDOW_S", "300")
    assert multiturn.session_window_s() == 300.0
    # 0 is allowed and means "disable rotation" (fixed id).
    monkeypatch.setenv("HERMES_SESSION_WINDOW_S", "0")
    assert multiturn.session_window_s() == 0.0
    # Negative / garbage falls back to the default.
    monkeypatch.setenv("HERMES_SESSION_WINDOW_S", "-1")
    assert multiturn.session_window_s() == float(multiturn.DEFAULT_SESSION_WINDOW_S)
    monkeypatch.setenv("HERMES_SESSION_WINDOW_S", "x")
    assert multiturn.session_window_s() == float(multiturn.DEFAULT_SESSION_WINDOW_S)


def test_new_session_id_namespaces_and_is_unique():
    a = multiturn.new_session_id("stackchan-voice")
    b = multiturn.new_session_id("stackchan-voice")
    assert a.startswith("stackchan-voice-")
    assert b.startswith("stackchan-voice-")
    assert a != b  # the uuid suffix makes each conversation distinct


# ---- MultiturnSession -----------------------------------------------------


def test_session_note_and_reset():
    s = MultiturnSession()
    assert s.turn_count == 0
    s.note_continuation(100.0)
    s.note_continuation(101.0)
    assert s.turn_count == 2
    assert s.last_activity == 101.0
    s.session_id = "conv-1"
    s.reset()
    assert s.turn_count == 0
    # reset leaves last_activity (harmless; the counter is the gate) and
    # the Hermes session_id (a brief silence still continues the same
    # conversation within the window).
    assert s.session_id == "conv-1"


def test_session_is_gap_stale():
    s = MultiturnSession()
    # No open gap → never stale.
    assert s.is_gap_stale(now=1_000.0, timeout_s=60.0) is False
    s.note_continuation(now=100.0)
    assert s.is_gap_stale(now=120.0, timeout_s=60.0) is False  # 20s < 60s
    assert s.is_gap_stale(now=200.0, timeout_s=60.0) is True  # 100s > 60s


# ---- conversation_id rotation (Phase 2) ----------------------------------


def _counter():
    """A deterministic id minter — id-1, id-2, … in call order."""
    seq = {"n": 0}

    def mint() -> str:
        seq["n"] += 1
        return f"id-{seq['n']}"

    return mint


def test_conversation_id_mints_when_none_open():
    s = MultiturnSession()
    cid = s.conversation_id(now=10.0, window_s=180.0, mint=_counter())
    assert cid == "id-1"
    assert s.session_id == "id-1"


def test_conversation_id_reuses_within_window():
    s = MultiturnSession()
    mint = _counter()
    first = s.conversation_id(now=10.0, window_s=180.0, mint=mint)
    s.last_activity = 10.0  # the caller stamps activity each turn
    # A follow-up 100 s later (< 180 s window) keeps the same id.
    again = s.conversation_id(now=110.0, window_s=180.0, mint=mint)
    assert first == again == "id-1"


def test_conversation_id_rotates_after_window():
    s = MultiturnSession()
    mint = _counter()
    first = s.conversation_id(now=10.0, window_s=180.0, mint=mint)
    s.last_activity = 10.0
    # 200 s later (> 180 s window) → a fresh conversation, new id.
    rotated = s.conversation_id(now=210.0, window_s=180.0, mint=mint)
    assert first == "id-1"
    assert rotated == "id-2"
    assert s.session_id == "id-2"


def test_conversation_id_window_zero_disables_rotation():
    s = MultiturnSession()
    mint = _counter()
    # window 0 never mints — the id stays empty so the caller falls back
    # to the fixed base id (restoring the pre-Phase-2 fixed-id behaviour).
    assert s.conversation_id(now=10.0, window_s=0.0, mint=mint) == ""
    s.last_activity = 10.0
    assert s.conversation_id(now=10_000.0, window_s=0.0, mint=mint) == ""
    assert s.session_id == ""


# ---- should_continue policy matrix ---------------------------------------


def _base(**over):
    """A continuing baseline; override one field per case."""
    kw = dict(
        enabled=True,
        route="hermes",
        reply="元気？",
        turn_count=0,
        max_turns=4,
        device_connected=True,
        muted=False,
        recording=False,
    )
    kw.update(over)
    return kw


def test_should_continue_happy_path():
    assert should_continue(**_base()) is True


def test_should_continue_blocked_when_disabled():
    assert should_continue(**_base(enabled=False)) is False


def test_should_continue_local_route_excluded():
    assert should_continue(**_base(route="local")) is False


def test_should_continue_needs_question_mark():
    assert should_continue(**_base(reply="そうだね。")) is False


def test_should_continue_respects_ceiling():
    assert should_continue(**_base(turn_count=4, max_turns=4)) is False
    assert should_continue(**_base(turn_count=3, max_turns=4)) is True


def test_should_continue_blocked_when_disconnected():
    assert should_continue(**_base(device_connected=False)) is False


def test_should_continue_blocked_when_muted():
    assert should_continue(**_base(muted=True)) is False


def test_should_continue_blocked_when_recording():
    assert should_continue(**_base(recording=True)) is False
