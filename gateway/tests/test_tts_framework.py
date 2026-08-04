"""Tests for the TTS framework skeleton (Issue #70 PR1).

Concrete engine implementations land in follow-up PRs; here we only
exercise the abstract base, the registry, and the orchestrator's
validation / error surface.
"""

from __future__ import annotations

import pytest

from stackchan_mcp.tts import (
    DEFAULT_VOICE,
    EngineRegistry,
    TTSEngine,
    get_registry,
    synthesize_and_send,
    warmup_engines,
)
from stackchan_mcp.tts.orchestrator import DEFAULT_VOICE_ENV, resolve_default_voice


class _FakeEngine(TTSEngine):
    """Minimal in-test engine used to exercise registry behaviour."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def synthesize(self, text: str, **opts: object) -> bytes:
        self.calls.append((text, dict(opts)))
        return b""


def test_tts_engine_is_abstract():
    """TTSEngine cannot be instantiated directly."""
    with pytest.raises(TypeError):
        TTSEngine()  # type: ignore[abstract]


def test_registry_rejects_engine_with_empty_name():
    """Registering an engine without a name is a programmer error."""
    reg = EngineRegistry()
    engine = _FakeEngine(name="")
    with pytest.raises(ValueError):
        reg.register(engine)


def test_registry_register_get_names_roundtrip():
    """register/get/names form a consistent set."""
    reg = EngineRegistry()
    engine = _FakeEngine(name="voicevox")

    reg.register(engine)

    assert reg.get("voicevox") is engine
    assert reg.get("nonexistent") is None
    assert reg.names() == ["voicevox"]


def test_registry_register_replaces_same_name():
    """Re-registering the same name swaps the engine — useful for tests."""
    reg = EngineRegistry()
    first = _FakeEngine(name="voicevox")
    second = _FakeEngine(name="voicevox")

    reg.register(first)
    reg.register(second)

    assert reg.get("voicevox") is second
    assert reg.names() == ["voicevox"]


def test_registry_names_are_sorted():
    """names() is sorted so callers (e.g. error messages) get a stable order."""
    reg = EngineRegistry()
    reg.register(_FakeEngine(name="zeta"))
    reg.register(_FakeEngine(name="alpha"))
    reg.register(_FakeEngine(name="mu"))

    assert reg.names() == ["alpha", "mu", "zeta"]


def test_get_registry_returns_singleton():
    """The default registry is process-wide (a singleton)."""
    assert get_registry() is get_registry()


def test_default_voice_constant():
    """The default voice is the planned VOICEVOX engine."""
    assert DEFAULT_VOICE == "voicevox"


@pytest.mark.asyncio
async def test_synthesize_and_send_rejects_missing_text():
    """No text -> ValueError before any engine lookup."""
    reg = EngineRegistry()
    with pytest.raises(ValueError, match="text"):
        await synthesize_and_send({}, registry=reg)


@pytest.mark.asyncio
async def test_synthesize_and_send_rejects_empty_text():
    """Whitespace-only text is rejected the same as empty."""
    reg = EngineRegistry()
    with pytest.raises(ValueError, match="text"):
        await synthesize_and_send({"text": "   "}, registry=reg)


@pytest.mark.asyncio
async def test_synthesize_and_send_rejects_non_string_text():
    """Non-string text -> ValueError (defensive against bad MCP clients)."""
    reg = EngineRegistry()
    with pytest.raises(ValueError, match="text"):
        await synthesize_and_send({"text": 42}, registry=reg)


@pytest.mark.asyncio
async def test_synthesize_and_send_unregistered_voice_raises():
    """Unregistered voice -> NotImplementedError, listing what's available."""
    reg = EngineRegistry()
    with pytest.raises(NotImplementedError) as exc_info:
        await synthesize_and_send({"text": "hello"}, registry=reg)

    msg = str(exc_info.value)
    assert "voicevox" in msg
    assert "(none)" in msg


@pytest.mark.asyncio
async def test_synthesize_and_send_requires_gateway():
    """Validation passes but pipeline refuses without a gateway argument.

    Surfacing a clear RuntimeError beats silently synthesising PCM that
    has nowhere to go. Validation tests can still exercise the
    argument-shape surface without spinning up a Gateway.
    """
    reg = EngineRegistry()
    reg.register(_FakeEngine(name="voicevox"))

    with pytest.raises(RuntimeError, match="gateway"):
        await synthesize_and_send({"text": "hello"}, registry=reg)


@pytest.mark.asyncio
async def test_synthesize_and_send_voice_default_falls_back():
    """Empty/missing 'voice' falls back to DEFAULT_VOICE."""
    reg = EngineRegistry()

    # Empty string voice -> default
    with pytest.raises(NotImplementedError) as exc_info:
        await synthesize_and_send({"text": "hello", "voice": ""}, registry=reg)
    assert DEFAULT_VOICE in str(exc_info.value)

    # Non-string voice -> default (not a TypeError)
    with pytest.raises(NotImplementedError) as exc_info:
        await synthesize_and_send({"text": "hello", "voice": 123}, registry=reg)
    assert DEFAULT_VOICE in str(exc_info.value)


# ---------------------------------------------------------------------------
# Runtime default-voice override (STACKCHAN_TTS_DEFAULT_VOICE)
# ---------------------------------------------------------------------------


def test_resolve_default_voice_defaults_to_voicevox(monkeypatch):
    """With the env unset, the runtime default is the built-in DEFAULT_VOICE."""
    monkeypatch.delenv(DEFAULT_VOICE_ENV, raising=False)
    assert resolve_default_voice() == DEFAULT_VOICE == "voicevox"


def test_resolve_default_voice_honors_env(monkeypatch):
    """The env var overrides the runtime default engine."""
    monkeypatch.setenv(DEFAULT_VOICE_ENV, "piper")
    assert resolve_default_voice() == "piper"


def test_resolve_default_voice_blank_env_falls_back(monkeypatch):
    """A blank/whitespace override is ignored (falls back to the default)."""
    monkeypatch.setenv(DEFAULT_VOICE_ENV, "   ")
    assert resolve_default_voice() == DEFAULT_VOICE


@pytest.mark.asyncio
async def test_synthesize_and_send_uses_env_default_voice(monkeypatch):
    """With no explicit 'voice', the env default drives engine lookup."""
    monkeypatch.setenv(DEFAULT_VOICE_ENV, "piper")
    reg = EngineRegistry()  # empty -> lookup for 'piper' fails, naming it

    with pytest.raises(NotImplementedError) as exc_info:
        await synthesize_and_send({"text": "hello"}, registry=reg)

    assert "piper" in str(exc_info.value)


@pytest.mark.asyncio
async def test_explicit_voice_wins_over_env_default(monkeypatch):
    """An explicit 'voice' argument beats the env default."""
    monkeypatch.setenv(DEFAULT_VOICE_ENV, "piper")
    reg = EngineRegistry()

    with pytest.raises(NotImplementedError) as exc_info:
        await synthesize_and_send(
            {"text": "hello", "voice": "voicevox"}, registry=reg
        )

    msg = str(exc_info.value)
    assert "voicevox" in msg
    assert "piper" not in msg


@pytest.mark.asyncio
async def test_synthesize_and_send_lists_available_engines_in_error():
    """Error message names what *is* registered so callers can pick correctly."""
    reg = EngineRegistry()
    reg.register(_FakeEngine(name="alpha"))
    reg.register(_FakeEngine(name="beta"))

    with pytest.raises(NotImplementedError) as exc_info:
        await synthesize_and_send(
            {"text": "hello", "voice": "voicevox"}, registry=reg
        )

    msg = str(exc_info.value)
    assert "alpha" in msg
    assert "beta" in msg


# ---------------------------------------------------------------------------
# Startup warm-up
# ---------------------------------------------------------------------------


class _WarmupEngine(_FakeEngine):
    """Engine that records warm-up calls, optionally failing."""

    def __init__(self, name: str, *, fail: bool = False) -> None:
        super().__init__(name)
        self._fail = fail
        self.warmups = 0

    def warmup(self) -> None:
        self.warmups += 1
        if self._fail:
            raise RuntimeError(f"{self.name} model missing")


def test_engine_warmup_defaults_to_noop():
    """Engines with nothing to preload inherit a no-op warm-up."""
    engine = _FakeEngine(name="http-backed")
    engine.warmup()  # must not raise


def test_warmup_engines_warms_every_registered_engine():
    reg = EngineRegistry()
    first = _WarmupEngine("alpha")
    second = _WarmupEngine("beta")
    reg.register(first)
    reg.register(second)

    warmup_engines(reg)

    assert (first.warmups, second.warmups) == (1, 1)


def test_warmup_engines_swallows_failure_and_continues():
    """One broken engine must not stop startup, nor the other engines.

    The gateway is still fully usable for the device connection and
    every non-TTS tool, so a warm-up failure is logged, not raised.
    """
    reg = EngineRegistry()
    broken = _WarmupEngine("broken", fail=True)
    healthy = _WarmupEngine("healthy")
    reg.register(broken)
    reg.register(healthy)

    warmup_engines(reg)  # must not raise

    assert healthy.warmups == 1


def test_warmup_engines_on_empty_registry_is_noop():
    warmup_engines(EngineRegistry())  # must not raise
