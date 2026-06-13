"""Tests for the gateway audio_stream helpers (Issue #70 PR2 / Issue #91)."""

from __future__ import annotations

import pytest

from stackchan_mcp.audio_stream import (
    handle_audio_frame,
    is_recording,
    push_opus_frames,
    start_recording,
    stop_recording,
)


class _FakeESP32:
    def __init__(self, *, fail_after: int | None = None) -> None:
        self.frames: list[bytes] = []
        self._fail_after = fail_after

    async def send_audio_frame(self, frame: bytes) -> None:
        if self._fail_after is not None and len(self.frames) >= self._fail_after:
            raise ConnectionError("simulated mid-stream disconnect")
        self.frames.append(frame)


@pytest.fixture(autouse=True)
def _cleanup_recording_slot():
    """Always release the module-level recording slot between tests."""
    yield
    if is_recording():
        stop_recording()


@pytest.mark.asyncio
async def test_handle_audio_frame_discards_when_no_recording():
    """Frames are discarded when no recording slot is open (Issue #91)."""
    assert not is_recording()
    # Should not raise; no buffer to grow.
    await handle_audio_frame(b"\x00\x01\x02", session_id="session-1")
    assert not is_recording()


@pytest.mark.asyncio
async def test_recording_lifecycle_buffers_frames_between_start_and_stop():
    """start_recording -> handle_audio_frame -> stop_recording returns the bytes.

    Outside the start/stop window, frames are silently discarded as
    before; inside it, the orchestrator collects them for the STT
    pipeline.
    """
    assert not is_recording()
    start_recording("session-listen")
    assert is_recording()

    await handle_audio_frame(b"frame-1", session_id="session-listen")
    await handle_audio_frame(b"frame-2", session_id="session-listen")
    await handle_audio_frame(b"frame-3", session_id="session-listen")

    frames = stop_recording()

    assert frames == [b"frame-1", b"frame-2", b"frame-3"]
    assert not is_recording()
    # A second stop returns an empty list rather than raising.
    assert stop_recording() == []


@pytest.mark.asyncio
async def test_handle_audio_frame_drops_frames_from_other_session():
    """Frames from a session other than the recording's are discarded.

    When ESP32 reconnects, ``ESP32Manager._handler`` swaps in a new
    connection and marks the old one disconnected, but the old
    socket's ``async for message in ws`` loop can still drain a
    binary frame or two before the close fully lands. Without
    session-id matching, those stale frames would land in the new
    session's recording buffer and corrupt the transcription.
    """
    start_recording("session-current")

    # Frame from the current session — buffered.
    await handle_audio_frame(b"current-frame", session_id="session-current")
    # Stale frame from the previous (now-disconnected) session.
    await handle_audio_frame(b"stale-frame", session_id="session-old")
    # Another current-session frame — still buffered.
    await handle_audio_frame(b"current-frame-2", session_id="session-current")

    frames = stop_recording()
    assert frames == [b"current-frame", b"current-frame-2"]


@pytest.mark.asyncio
async def test_start_recording_resets_previous_buffer():
    """Re-opening the slot drops any frames buffered from a leaked prior run.

    The listen_lock should prevent this in practice, but the audio
    pipeline should still be defensive — leaking frames from one
    capture into the next would mix transcriptions silently.
    """
    start_recording("session-a")
    await handle_audio_frame(b"leftover", session_id="session-a")

    # Without an intervening stop_recording (simulating a crashed
    # prior call), opening the slot afresh discards the leftover.
    start_recording("session-b")
    await handle_audio_frame(b"new-1", session_id="session-b")

    frames = stop_recording()
    assert frames == [b"new-1"]


@pytest.mark.asyncio
async def test_push_opus_frames_sends_each_frame_in_order():
    """All frames reach the device in the order they were given."""
    esp32 = _FakeESP32()
    frames = [b"a", b"b", b"c"]

    sent = await push_opus_frames(esp32, frames)

    assert sent == 3
    assert esp32.frames == frames


@pytest.mark.asyncio
async def test_push_opus_frames_propagates_disconnect():
    """A mid-stream ConnectionError surfaces, with the partial count visible."""
    esp32 = _FakeESP32(fail_after=2)
    frames = [b"a", b"b", b"c", b"d"]

    with pytest.raises(ConnectionError):
        await push_opus_frames(esp32, frames)

    # The first two frames did make it to the device before the failure.
    assert esp32.frames == [b"a", b"b"]


@pytest.mark.asyncio
async def test_push_opus_frames_empty_iterable_returns_zero():
    """Pushing zero frames is a no-op rather than an error."""
    esp32 = _FakeESP32()

    sent = await push_opus_frames(esp32, [])

    assert sent == 0
    assert esp32.frames == []


# ---- Phase F: live mic input level -----------------------------------

import math  # noqa: E402

import stackchan_mcp.audio_stream as audio_stream  # noqa: E402
from stackchan_mcp.audio_stream import (  # noqa: E402
    _rms_from_pcm16,
    get_input_level,
)


@pytest.fixture(autouse=True)
def _reset_level_state():
    """Reset the module-level meter state + decoder between tests."""
    yield
    audio_stream._last_level = 0.0
    audio_stream._level_decoder = None


def _encode_frame(pcm_frame: bytes) -> bytes:
    """Encode one 60ms/16kHz/mono PCM frame to Opus (real libopus)."""
    opuslib = pytest.importorskip("opuslib")
    from stackchan_mcp.stt.audio_utils import (
        DEVICE_CHANNELS,
        DEVICE_SAMPLE_RATE,
        SAMPLES_PER_FRAME,
    )

    encoder = opuslib.Encoder(
        DEVICE_SAMPLE_RATE, DEVICE_CHANNELS, opuslib.APPLICATION_VOIP
    )
    return encoder.encode(pcm_frame, SAMPLES_PER_FRAME)


def test_rms_from_pcm16_silence_is_zero():
    from stackchan_mcp.stt.audio_utils import SAMPLES_PER_FRAME

    assert _rms_from_pcm16(b"\x00\x00" * SAMPLES_PER_FRAME) == 0.0


def test_rms_from_pcm16_full_scale_is_one():
    import struct

    from stackchan_mcp.stt.audio_utils import SAMPLES_PER_FRAME

    # Constant -32768 (full negative scale) → RMS = 32768 → normalised 1.0.
    pcm = struct.pack(f"<{SAMPLES_PER_FRAME}h", *([-32768] * SAMPLES_PER_FRAME))
    assert _rms_from_pcm16(pcm) == pytest.approx(1.0, abs=1e-6)


def test_rms_from_pcm16_empty_and_odd_bytes():
    assert _rms_from_pcm16(b"") == 0.0
    # A lone trailing byte is dropped rather than crashing frombytes.
    assert _rms_from_pcm16(b"\x10") == 0.0


def test_get_input_level_zero_when_idle():
    assert not is_recording()
    assert get_input_level() == 0.0


@pytest.mark.asyncio
async def test_level_updates_on_recorded_frame():
    """A buffered frame updates the live level above zero for loud audio."""
    pytest.importorskip("opuslib")
    import struct

    from stackchan_mcp.stt.audio_utils import (
        DEVICE_SAMPLE_RATE,
        SAMPLES_PER_FRAME,
    )

    # A loud-ish sine so the decoded RMS is clearly non-zero.
    samples = [
        int(20000 * math.sin(2 * math.pi * 440 * n / DEVICE_SAMPLE_RATE))
        for n in range(SAMPLES_PER_FRAME)
    ]
    pcm = struct.pack(f"<{SAMPLES_PER_FRAME}h", *samples)
    frame = _encode_frame(pcm)

    start_recording("sess-level")
    try:
        assert get_input_level() == 0.0  # reset on start
        await handle_audio_frame(frame, session_id="sess-level")
        level = get_input_level()
    finally:
        stop_recording()

    assert 0.0 < level <= 1.0
    # Level is reset to 0.0 when the recording closes.
    assert get_input_level() == 0.0


@pytest.mark.asyncio
async def test_level_not_updated_without_recording():
    """Frames outside a recording slot leave the level at zero."""
    pytest.importorskip("opuslib")
    frame = _encode_frame(b"\x00\x00" * 960)
    assert not is_recording()
    await handle_audio_frame(frame, session_id="sess-x")
    assert get_input_level() == 0.0


@pytest.mark.asyncio
async def test_level_survives_undecodable_frame():
    """A frame that fails to decode leaves the previous level intact."""
    start_recording("sess-bad")
    try:
        audio_stream._last_level = 0.3  # pretend a prior good frame
        await handle_audio_frame(b"\x99\x99\x99\xff", session_id="sess-bad")
        # Garbage → _frame_rms_level returns None → level unchanged.
        assert get_input_level() == 0.3
    finally:
        stop_recording()
