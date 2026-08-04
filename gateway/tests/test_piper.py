"""Tests for the Piper engine (yorishiro fork, English TTS).

Piper runs in-process via ``piper-tts`` (onnxruntime), but the unit
suite must not depend on that package or on a real ``.onnx`` voice
model. We cut the dependency the same way ``test_voicevox`` cuts httpx:
:class:`PiperEngine` takes a ``voice_loader`` argument, so tests inject a
fake loader that returns a stub voice producing known PCM. Both Piper
API generations (legacy raw-stream and modern chunk-generator) are
exercised with fakes shaped like each.
"""

from __future__ import annotations

import array
import threading

import pytest

from stackchan_mcp.tts.audio_utils import DEVICE_SAMPLE_RATE
from stackchan_mcp.tts.piper import (
    DEFAULT_TIMEOUT_S,
    PIPER_MODEL_ENV,
    PIPER_TIMEOUT_ENV,
    PiperEngine,
    _resolve_timeout_s,
    _voice_to_pcm,
)


def _int16_bytes(samples: list[int]) -> bytes:
    """Pack int16 samples into signed-16-bit LE bytes."""
    return array.array("h", samples).tobytes()


class _ModernChunk:
    """Stub of the modern ``piper.AudioChunk`` (>=1.3)."""

    def __init__(self, pcm: bytes, sample_rate: int) -> None:
        self.audio_int16_bytes = pcm
        self.sample_rate = sample_rate


class _ModernVoice:
    """Stub voice exposing the modern generator API (``synthesize``).

    Deliberately has no ``synthesize_stream_raw`` so ``_voice_to_pcm``
    takes the modern branch.
    """

    def __init__(self, pcm: bytes, sample_rate: int, *, chunks: int = 2) -> None:
        self._pcm = pcm
        self._sample_rate = sample_rate
        self._chunks = chunks
        self.calls: list[str] = []

    def synthesize(self, text: str):
        self.calls.append(text)
        # Split the PCM across N chunks to mimic streaming synthesis.
        step = max(2, (len(self._pcm) // self._chunks) & ~1)  # keep 16-bit aligned
        for i in range(0, len(self._pcm), step):
            yield _ModernChunk(self._pcm[i : i + step], self._sample_rate)


class _LegacyConfig:
    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate


class _LegacyVoice:
    """Stub voice exposing the legacy raw-stream API."""

    def __init__(self, pcm: bytes, sample_rate: int) -> None:
        self._pcm = pcm
        self.config = _LegacyConfig(sample_rate)
        self.calls: list[str] = []

    def synthesize_stream_raw(self, text: str):
        self.calls.append(text)
        yield self._pcm


# ---------------------------------------------------------------------------
# Defaults / configuration
# ---------------------------------------------------------------------------


def test_engine_name_is_piper():
    """The registry uses ``name`` to look up engines from the say tool."""
    engine = PiperEngine(model_path="/models/en_US-lessac-medium.onnx")
    assert engine.name == "piper"


def test_model_path_from_env(monkeypatch):
    """Model path falls back to the STACKCHAN_PIPER_MODEL env var."""
    monkeypatch.setenv(PIPER_MODEL_ENV, "/env/voice.onnx")
    engine = PiperEngine()
    assert engine.model_path == "/env/voice.onnx"


def test_model_path_arg_overrides_env(monkeypatch):
    """Constructor argument wins over the environment variable."""
    monkeypatch.setenv(PIPER_MODEL_ENV, "/env/voice.onnx")
    engine = PiperEngine(model_path="/arg/voice.onnx")
    assert engine.model_path == "/arg/voice.onnx"


# ---------------------------------------------------------------------------
# _voice_to_pcm — both API generations
# ---------------------------------------------------------------------------


def test_voice_to_pcm_modern_api():
    """Modern generator API: bytes concatenated, rate from the chunk."""
    pcm = _int16_bytes([100, 200, 300, 400])
    voice = _ModernVoice(pcm, 22050)

    rate, out = _voice_to_pcm(voice, "hello")

    assert rate == 22050
    assert out == pcm


def test_voice_to_pcm_legacy_api():
    """Legacy raw-stream API: rate from voice.config.sample_rate."""
    pcm = _int16_bytes([1, 2, 3, 4])
    voice = _LegacyVoice(pcm, 16000)

    rate, out = _voice_to_pcm(voice, "hello")

    assert rate == 16000
    assert out == pcm


def test_voice_to_pcm_modern_no_chunks_raises():
    """A modern voice that yields nothing is a clear error, not silent b''."""

    class _EmptyVoice:
        def synthesize(self, text: str):
            return iter(())

    with pytest.raises(RuntimeError, match="no audio"):
        _voice_to_pcm(_EmptyVoice(), "hello")


# ---------------------------------------------------------------------------
# synthesize() pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthesize_modern_resamples_to_device_rate():
    """22.05 kHz Piper output is resampled down to the device's 16 kHz."""
    # 100 ms @ 22050 Hz = 2205 samples.
    pcm = _int16_bytes([i % 100 for i in range(2205)])
    voice = _ModernVoice(pcm, 22050)
    engine = PiperEngine(model_path="x", voice_loader=lambda _p: voice)

    out = await engine.synthesize("hello world")

    decoded = array.array("h")
    decoded.frombytes(out)
    # 2205 * 16000 / 22050 = 1600 samples; allow interpolation slack.
    assert 1590 <= len(decoded) <= 1610


@pytest.mark.asyncio
async def test_synthesize_no_resample_when_already_device_rate():
    """A 16 kHz voice needs no resampling — PCM passes through unchanged."""
    pcm = _int16_bytes([i % 50 for i in range(800)])  # 50 ms @ 16 kHz
    voice = _ModernVoice(pcm, DEVICE_SAMPLE_RATE)
    engine = PiperEngine(model_path="x", voice_loader=lambda _p: voice)

    out = await engine.synthesize("hi")

    assert out == pcm


@pytest.mark.asyncio
async def test_synthesize_legacy_api_end_to_end():
    """The legacy raw-stream voice also flows through synthesize()."""
    pcm = _int16_bytes([i % 30 for i in range(320)])  # 20 ms @ 16 kHz
    voice = _LegacyVoice(pcm, DEVICE_SAMPLE_RATE)
    engine = PiperEngine(model_path="x", voice_loader=lambda _p: voice)

    out = await engine.synthesize("hi there")

    assert out == pcm
    assert voice.calls == ["hi there"]


@pytest.mark.asyncio
async def test_synthesize_rejects_empty_text():
    """Empty/whitespace text fails fast before the model is even loaded."""
    load_calls: list[str] = []

    def loader(path: str):
        load_calls.append(path)
        return _ModernVoice(b"\x00\x00", DEVICE_SAMPLE_RATE)

    engine = PiperEngine(model_path="x", voice_loader=loader)

    with pytest.raises(ValueError, match="text"):
        await engine.synthesize("   ")

    assert load_calls == []  # never loaded the model


@pytest.mark.asyncio
async def test_voice_loaded_once_and_cached():
    """The model is loaded lazily on first use and reused across calls."""
    load_calls: list[str] = []

    def loader(path: str):
        load_calls.append(path)
        return _ModernVoice(_int16_bytes([1, 2, 3, 4]), DEVICE_SAMPLE_RATE)

    engine = PiperEngine(model_path="/models/v.onnx", voice_loader=loader)

    await engine.synthesize("one")
    await engine.synthesize("two")

    assert load_calls == ["/models/v.onnx"]  # loaded exactly once


@pytest.mark.asyncio
async def test_synthesize_without_model_path_raises(monkeypatch):
    """No model path configured -> a clear error naming the env var."""
    monkeypatch.delenv(PIPER_MODEL_ENV, raising=False)
    engine = PiperEngine(model_path=None)  # real loader, but never reached

    with pytest.raises(RuntimeError, match=PIPER_MODEL_ENV):
        await engine.synthesize("hello")


# ---------------------------------------------------------------------------
# Warm-up (eager model load at gateway startup)
# ---------------------------------------------------------------------------


def test_warmup_loads_model_eagerly():
    """warmup() pays the load cost before any synthesize() call."""
    load_calls: list[str] = []

    def loader(path: str):
        load_calls.append(path)
        return _ModernVoice(_int16_bytes([1, 2, 3, 4]), DEVICE_SAMPLE_RATE)

    engine = PiperEngine(model_path="/models/v.onnx", voice_loader=loader)
    engine.warmup()

    assert load_calls == ["/models/v.onnx"]


@pytest.mark.asyncio
async def test_warmup_then_synthesize_does_not_reload():
    """A warmed engine reuses the cached voice on the first say()."""
    load_calls: list[str] = []

    def loader(path: str):
        load_calls.append(path)
        return _ModernVoice(_int16_bytes([1, 2, 3, 4]), DEVICE_SAMPLE_RATE)

    engine = PiperEngine(model_path="/models/v.onnx", voice_loader=loader)
    engine.warmup()
    await engine.synthesize("hello")

    assert load_calls == ["/models/v.onnx"]  # loaded exactly once


def test_warmup_without_model_path_is_noop(monkeypatch):
    """An unconfigured Piper must not make gateway startup fail.

    The engine registers whenever the ``piper`` package is importable,
    so a VOICEVOX-only user has it in the registry without ever setting
    a model path.
    """
    monkeypatch.delenv(PIPER_MODEL_ENV, raising=False)
    engine = PiperEngine(model_path=None)  # real loader, must not be reached

    engine.warmup()  # must not raise


def test_warmup_propagates_loader_failure():
    """A configured-but-broken model surfaces to the caller.

    ``warmup_engines`` is what decides this is non-fatal; the engine
    itself reports honestly.
    """

    def loader(path: str):
        raise RuntimeError("onnxruntime failed to load")

    engine = PiperEngine(model_path="/models/v.onnx", voice_loader=loader)

    with pytest.raises(RuntimeError, match="onnxruntime failed to load"):
        engine.warmup()


# ---------------------------------------------------------------------------
# Synthesis timeout
# ---------------------------------------------------------------------------


def test_timeout_defaults_when_env_unset(monkeypatch):
    monkeypatch.delenv(PIPER_TIMEOUT_ENV, raising=False)
    assert _resolve_timeout_s() == DEFAULT_TIMEOUT_S


def test_timeout_env_override(monkeypatch):
    monkeypatch.setenv(PIPER_TIMEOUT_ENV, "12.5")
    assert _resolve_timeout_s() == 12.5


@pytest.mark.parametrize("raw", ["abc", "0", "-1", "   "])
def test_timeout_falls_back_on_unusable_value(monkeypatch, raw):
    """A bad override must not make say() unusable."""
    monkeypatch.setenv(PIPER_TIMEOUT_ENV, raw)
    assert _resolve_timeout_s() == DEFAULT_TIMEOUT_S


@pytest.mark.asyncio
async def test_synthesize_times_out_with_actionable_error(monkeypatch):
    """A wedged model load returns a readable error, not a silent stall.

    This is the regression guard for the Windows report where the first
    say() hung until the MCP client's own timeout with nothing logged.
    """
    monkeypatch.setenv(PIPER_TIMEOUT_ENV, "0.05")
    release = threading.Event()

    def loader(path: str):
        release.wait(timeout=5.0)  # stand-in for a stuck native import
        return _ModernVoice(_int16_bytes([1, 2]), DEVICE_SAMPLE_RATE)

    engine = PiperEngine(model_path="/models/stuck.onnx", voice_loader=loader)

    try:
        with pytest.raises(RuntimeError, match="timed out"):
            await engine.synthesize("hello")
    finally:
        # Let the worker thread finish; to_thread cannot be cancelled.
        release.set()


@pytest.mark.asyncio
async def test_timeout_error_names_the_model_and_env_var(monkeypatch):
    """The error has to tell the operator what to look at next."""
    monkeypatch.setenv(PIPER_TIMEOUT_ENV, "0.05")
    release = threading.Event()

    def loader(path: str):
        release.wait(timeout=5.0)
        return _ModernVoice(_int16_bytes([1, 2]), DEVICE_SAMPLE_RATE)

    engine = PiperEngine(model_path="/models/stuck.onnx", voice_loader=loader)

    try:
        with pytest.raises(RuntimeError) as excinfo:
            await engine.synthesize("hello")
    finally:
        release.set()

    message = str(excinfo.value)
    assert "/models/stuck.onnx" in message
    assert PIPER_TIMEOUT_ENV in message
