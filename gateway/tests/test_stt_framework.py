"""Tests for the STT framework skeleton (Issue #91).

Mirrors :mod:`tests.test_tts_framework`: exercises the abstract base,
the registry, and the orchestrator's validation / error surface
without depending on the heavy ML engines.
"""

from __future__ import annotations

from typing import Any

import pytest

from stackchan_mcp.stt import (
    DEFAULT_ENGINE,
    EngineRegistry,
    STTEngine,
    get_registry,
    listen_and_transcribe,
    warmup_engines,
)
from stackchan_mcp.stt.orchestrator import (
    DEFAULT_LANGUAGE,
    DEFAULT_LANGUAGE_ENV,
    resolve_default_language,
)


class _FakeEngine(STTEngine):
    """Minimal in-test engine used to exercise registry behaviour."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple[bytes, dict[str, Any]]] = []

    async def transcribe(self, pcm: bytes, **opts: Any) -> dict[str, Any]:
        self.calls.append((pcm, dict(opts)))
        return {"text": "", "language": ""}


def test_stt_engine_is_abstract():
    """STTEngine cannot be instantiated directly."""
    with pytest.raises(TypeError):
        STTEngine()  # type: ignore[abstract]


def test_registry_rejects_engine_with_empty_name():
    """Registering an engine without a name is a programmer error."""
    reg = EngineRegistry()
    engine = _FakeEngine(name="")
    with pytest.raises(ValueError):
        reg.register(engine)


def test_registry_register_get_names_roundtrip():
    """register/get/names form a consistent set."""
    reg = EngineRegistry()
    engine = _FakeEngine(name="faster-whisper")

    reg.register(engine)

    assert reg.get("faster-whisper") is engine
    assert reg.get("nonexistent") is None
    assert reg.names() == ["faster-whisper"]


def test_registry_register_replaces_same_name():
    """Re-registering the same name swaps the engine — useful for tests."""
    reg = EngineRegistry()
    first = _FakeEngine(name="faster-whisper")
    second = _FakeEngine(name="faster-whisper")

    reg.register(first)
    reg.register(second)

    assert reg.get("faster-whisper") is second
    assert reg.names() == ["faster-whisper"]


def test_registry_names_are_sorted():
    """names() is sorted so error messages get a stable order."""
    reg = EngineRegistry()
    reg.register(_FakeEngine(name="zeta"))
    reg.register(_FakeEngine(name="alpha"))
    reg.register(_FakeEngine(name="mu"))

    assert reg.names() == ["alpha", "mu", "zeta"]


def test_get_registry_returns_singleton():
    """The default registry is process-wide (a singleton)."""
    assert get_registry() is get_registry()


def test_default_engine_constant():
    """The default engine is the planned faster-whisper local engine."""
    assert DEFAULT_ENGINE == "faster-whisper"


@pytest.mark.asyncio
async def test_listen_rejects_non_int_duration():
    """Non-integer duration_ms -> ValueError before any engine lookup."""
    reg = EngineRegistry()
    with pytest.raises(ValueError, match="duration_ms"):
        await listen_and_transcribe(
            {"duration_ms": "5000"}, registry=reg
        )


@pytest.mark.asyncio
async def test_listen_rejects_boolean_duration():
    """``bool`` is a subclass of int — guard against it explicitly."""
    reg = EngineRegistry()
    with pytest.raises(ValueError, match="duration_ms"):
        await listen_and_transcribe(
            {"duration_ms": True}, registry=reg
        )


@pytest.mark.asyncio
async def test_listen_rejects_duration_below_minimum():
    """duration_ms < 100 -> ValueError."""
    reg = EngineRegistry()
    with pytest.raises(ValueError, match="duration_ms"):
        await listen_and_transcribe(
            {"duration_ms": 50}, registry=reg
        )


@pytest.mark.asyncio
async def test_listen_rejects_duration_above_maximum():
    """duration_ms > 30000 -> ValueError."""
    reg = EngineRegistry()
    with pytest.raises(ValueError, match="duration_ms"):
        await listen_and_transcribe(
            {"duration_ms": 60000}, registry=reg
        )


@pytest.mark.asyncio
async def test_listen_unregistered_engine_raises():
    """Unregistered engine -> NotImplementedError, listing what's available."""
    reg = EngineRegistry()
    with pytest.raises(NotImplementedError) as exc_info:
        await listen_and_transcribe(
            {"duration_ms": 1000}, registry=reg
        )

    msg = str(exc_info.value)
    assert "faster-whisper" in msg
    assert "(none)" in msg


@pytest.mark.asyncio
async def test_listen_engine_default_falls_back():
    """Empty/missing 'engine' falls back to DEFAULT_ENGINE."""
    reg = EngineRegistry()

    # Empty string -> default
    with pytest.raises(NotImplementedError) as exc_info:
        await listen_and_transcribe(
            {"duration_ms": 1000, "engine": ""}, registry=reg
        )
    assert DEFAULT_ENGINE in str(exc_info.value)

    # Non-string -> default (not a TypeError)
    with pytest.raises(NotImplementedError) as exc_info:
        await listen_and_transcribe(
            {"duration_ms": 1000, "engine": 123}, registry=reg
        )
    assert DEFAULT_ENGINE in str(exc_info.value)


@pytest.mark.asyncio
async def test_listen_requires_gateway():
    """Validation passes but pipeline refuses without a gateway argument."""
    reg = EngineRegistry()
    reg.register(_FakeEngine(name="faster-whisper"))

    with pytest.raises(RuntimeError, match="gateway"):
        await listen_and_transcribe(
            {"duration_ms": 1000}, registry=reg
        )


@pytest.mark.asyncio
async def test_listen_lists_available_engines_in_error():
    """Error message names what *is* registered so callers can pick correctly."""
    reg = EngineRegistry()
    reg.register(_FakeEngine(name="alpha"))
    reg.register(_FakeEngine(name="beta"))

    with pytest.raises(NotImplementedError) as exc_info:
        await listen_and_transcribe(
            {"duration_ms": 1000, "engine": "faster-whisper"},
            registry=reg,
        )

    msg = str(exc_info.value)
    assert "alpha" in msg
    assert "beta" in msg


# ---------------------------------------------------------------------------
# Default recognition language
# ---------------------------------------------------------------------------


def test_resolve_default_language_defaults_to_japanese(monkeypatch):
    """The built-in default never changes silently."""
    monkeypatch.delenv(DEFAULT_LANGUAGE_ENV, raising=False)
    assert resolve_default_language() == DEFAULT_LANGUAGE


def test_resolve_default_language_honors_env(monkeypatch):
    monkeypatch.setenv(DEFAULT_LANGUAGE_ENV, "en")
    assert resolve_default_language() == "en"


def test_resolve_default_language_blank_env_falls_back(monkeypatch):
    monkeypatch.setenv(DEFAULT_LANGUAGE_ENV, "   ")
    assert resolve_default_language() == DEFAULT_LANGUAGE


def test_resolve_default_language_auto_means_autodetect(monkeypatch):
    """``auto`` reaches the engines' existing None = detect behaviour."""
    monkeypatch.setenv(DEFAULT_LANGUAGE_ENV, "auto")
    assert resolve_default_language() is None


def test_resolve_default_language_auto_is_case_insensitive(monkeypatch):
    monkeypatch.setenv(DEFAULT_LANGUAGE_ENV, "AUTO")
    assert resolve_default_language() is None


def test_resolve_default_language_strips_whitespace(monkeypatch):
    monkeypatch.setenv(DEFAULT_LANGUAGE_ENV, "  en  ")
    assert resolve_default_language() == "en"


# ---------------------------------------------------------------------------
# Startup warm-up
# ---------------------------------------------------------------------------


class _WarmupEngine(_FakeEngine):
    """Engine that records warm-up calls, optionally failing."""

    def __init__(self, name: str, *, fail: bool = False) -> None:
        super().__init__(name)
        self._fail = fail
        self.warmups = 0

    async def warmup(self) -> None:
        self.warmups += 1
        if self._fail:
            raise RuntimeError(f"{self.name} model unavailable")


@pytest.mark.asyncio
async def test_engine_warmup_defaults_to_noop():
    """Engines with nothing to preload inherit a no-op warm-up."""
    engine = _FakeEngine(name="remote-api")
    await engine.warmup()  # must not raise


@pytest.mark.asyncio
async def test_warmup_engines_warms_every_registered_engine():
    reg = EngineRegistry()
    first = _WarmupEngine("alpha")
    second = _WarmupEngine("beta")
    reg.register(first)
    reg.register(second)

    await warmup_engines(reg)

    assert (first.warmups, second.warmups) == (1, 1)


@pytest.mark.asyncio
async def test_warmup_engines_swallows_failure_and_continues():
    """A model that cannot load must not stop the gateway from starting."""
    reg = EngineRegistry()
    broken = _WarmupEngine("broken", fail=True)
    healthy = _WarmupEngine("healthy")
    reg.register(broken)
    reg.register(healthy)

    await warmup_engines(reg)  # must not raise

    assert healthy.warmups == 1


@pytest.mark.asyncio
async def test_warmup_engines_on_empty_registry_is_noop():
    await warmup_engines(EngineRegistry())  # must not raise
