"""Tests for the faster-whisper engine's recognition settings.

This module had no tests at all until now — the STT suite injects
fakes at the orchestrator level, so the concrete engine was a
structural blind spot (the same one that hid Piper's real voice
loader until it hung in production).

The heavy dependency is cut at the model: ``_load_model`` returns
``self._model`` when it is already set, so a fake model assigned
directly is enough to observe what reaches ``transcribe()``.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("faster_whisper")

from stackchan_mcp.stt.faster_whisper import (  # noqa: E402
    DEFAULT_BEAM_SIZE,
    FasterWhisperEngine,
    _parse_beam_size,
)

BEAM_ENV = "STACKCHAN_FASTER_WHISPER_BEAM_SIZE"
HOTWORDS_ENV = "STACKCHAN_FASTER_WHISPER_HOTWORDS"


# ---- _parse_beam_size -------------------------------------------------


def test_parse_beam_size_unset_uses_default():
    assert _parse_beam_size(None) == DEFAULT_BEAM_SIZE
    assert _parse_beam_size("   ") == DEFAULT_BEAM_SIZE


def test_parse_beam_size_reads_an_integer():
    assert _parse_beam_size("5") == 5
    assert _parse_beam_size(" 5 ") == 5


def test_parse_beam_size_falls_back_on_garbage():
    # A typo must not pin recognition quality to something nobody chose.
    assert _parse_beam_size("wide") == DEFAULT_BEAM_SIZE


def test_parse_beam_size_rejects_below_one():
    assert _parse_beam_size("0") == DEFAULT_BEAM_SIZE
    assert _parse_beam_size("-3") == DEFAULT_BEAM_SIZE


# ---- construction -----------------------------------------------------


def test_defaults_when_env_unset(monkeypatch):
    monkeypatch.delenv(BEAM_ENV, raising=False)
    monkeypatch.delenv(HOTWORDS_ENV, raising=False)
    engine = FasterWhisperEngine()
    assert engine._beam_size == DEFAULT_BEAM_SIZE
    assert engine._hotwords is None


def test_reads_settings_from_env(monkeypatch):
    monkeypatch.setenv(BEAM_ENV, "5")
    monkeypatch.setenv(HOTWORDS_ENV, "Saki, Dale")
    engine = FasterWhisperEngine()
    assert engine._beam_size == 5
    assert engine._hotwords == "Saki, Dale"


def test_blank_hotwords_mean_no_hint(monkeypatch):
    # faster-whisper wants None, not an empty prompt.
    monkeypatch.setenv(HOTWORDS_ENV, "   ")
    assert FasterWhisperEngine()._hotwords is None


def test_explicit_arguments_beat_env(monkeypatch):
    monkeypatch.setenv(BEAM_ENV, "5")
    monkeypatch.setenv(HOTWORDS_ENV, "from-env")
    engine = FasterWhisperEngine(hotwords="explicit", beam_size=3)
    assert engine._beam_size == 3
    assert engine._hotwords == "explicit"


# ---- what reaches transcribe() ---------------------------------------


class _FakeInfo:
    language = "en"
    language_probability = 0.99


class _FakeSegment:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeModel:
    """Stands in for ``WhisperModel``, recording the call it receives."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def transcribe(self, _audio: Any, **kwargs: Any) -> tuple[Any, Any]:
        self.kwargs = kwargs
        return iter([_FakeSegment("hello")]), _FakeInfo()


def _silence(ms: int = 100) -> bytes:
    """Signed-16-bit mono silence at the device sample rate."""
    from stackchan_mcp.stt.audio_utils import DEVICE_SAMPLE_RATE

    return b"\x00\x00" * int(DEVICE_SAMPLE_RATE * ms / 1000)


@pytest.mark.asyncio
async def test_settings_reach_the_model(monkeypatch):
    monkeypatch.setenv(BEAM_ENV, "5")
    monkeypatch.setenv(HOTWORDS_ENV, "Saki, Dale")
    engine = FasterWhisperEngine()
    fake = _FakeModel()
    engine._model = fake

    result = await engine.transcribe(_silence(), language="en")

    assert result["text"] == "hello"
    assert fake.kwargs["beam_size"] == 5
    assert fake.kwargs["hotwords"] == "Saki, Dale"


@pytest.mark.asyncio
async def test_unset_hotwords_passed_as_none(monkeypatch):
    # None is faster-whisper's own default; "" would be a real (empty)
    # prompt and is not the same thing.
    monkeypatch.delenv(HOTWORDS_ENV, raising=False)
    monkeypatch.delenv(BEAM_ENV, raising=False)
    engine = FasterWhisperEngine()
    fake = _FakeModel()
    engine._model = fake

    await engine.transcribe(_silence(), language="ja")

    assert fake.kwargs["hotwords"] is None
    assert fake.kwargs["beam_size"] == DEFAULT_BEAM_SIZE
