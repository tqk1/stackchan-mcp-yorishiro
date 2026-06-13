"""Opus audio frame handling for the gateway <-> device link.

Outbound (TTS) frames are produced by
:mod:`stackchan_mcp.tts.audio_utils` and pushed here to the connected
ESP32 via :meth:`stackchan_mcp.esp32_client.ESP32Manager.send_audio_frame`.

The inbound side (STT pipeline, Phase 4 / Issue #91) is now wired:
binary frames coming up from the device land in
:func:`handle_audio_frame`, which buffers them into a module-level
recording slot when one is active. The
:mod:`stackchan_mcp.stt.orchestrator` opens the slot via
:func:`start_recording` before sending ``listen.start`` to the device
and closes it via :func:`stop_recording` after the capture window;
outside an active recording, inbound frames are still discarded.

The recording slot is intentionally a module-level singleton: the
device's :class:`stackchan_mcp.esp32_client.ESP32Manager` only manages
one connection, and the STT orchestrator serialises ``listen()`` calls
through :attr:`ESP32Manager.listen_lock`, so concurrent captures
cannot race the buffer. If multi-device support lands later, this
should move onto the connection object.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from .esp32_client import ESP32Manager

logger = logging.getLogger(__name__)


# --- Recording slot (inbound STT capture) ---------------------------------
#
# A single capture at a time is enforced by the orchestrator's
# ``listen_lock``; this module only owns the buffer itself.

_recording_session_id: str | None = None
_recording_frames: list[bytes] = []


# --- Live mic input level (Phase F dashboard) -----------------------------
#
# The most recent inbound frame's RMS amplitude, normalised to 0.0-1.0,
# updated on every buffered frame in handle_audio_frame. It exists only
# so the dashboard can render a VU-style meter; it is reset to 0.0 when
# a recording opens/closes so a stale value never lingers after a turn.
# Full-scale s16 PCM RMS is 32768; we normalise against that.
_INT16_FULL_SCALE = 32768.0
_last_level: float = 0.0

#: A persistent Opus decoder reused across frames for the level meter.
#: Lazily created (the [stt] extra ships opuslib); decode failures are
#: swallowed so the meter never disturbs the capture path.
_level_decoder: Any = None


def start_recording(session_id: str) -> None:
    """Open a fresh recording slot for ``session_id``.

    Any frames already buffered are discarded so a previous call that
    crashed before ``stop_recording`` cannot leak into the next
    capture. The orchestrator wraps start/stop in a try/finally to
    guarantee the slot is closed even on error.
    """
    global _recording_session_id, _recording_frames, _last_level
    if _recording_session_id is not None:
        # Defensive: the lock should prevent this, but if it ever
        # fires we leak no audio — just log loudly so the regression
        # is visible.
        logger.warning(
            "start_recording called while session=%s was still active; "
            "dropping %d buffered frames",
            _recording_session_id,
            len(_recording_frames),
        )
    _recording_session_id = session_id
    _recording_frames = []
    _last_level = 0.0


def stop_recording() -> list[bytes]:
    """Close the recording slot and return the buffered Opus frames.

    Returns an empty list if no recording was active. The slot is
    cleared whether or not frames were captured so the next call to
    :func:`start_recording` starts clean.
    """
    global _recording_session_id, _recording_frames, _last_level
    frames = _recording_frames
    _recording_session_id = None
    _recording_frames = []
    _last_level = 0.0
    return frames


def is_recording() -> bool:
    """Return ``True`` when a recording slot is currently open."""
    return _recording_session_id is not None


def get_input_level() -> float:
    """Return the most recent inbound frame's RMS level (0.0-1.0).

    Reflects the last frame buffered during the active recording, or
    0.0 when no recording is open (the value is reset on start/stop).
    Used by the dashboard's mic-level meter (Phase F).
    """
    return _last_level


def _frame_rms_level(frame: bytes) -> float | None:
    """Decode one Opus frame and return its RMS amplitude (0.0-1.0).

    Returns None when the frame cannot be decoded or opuslib is not
    installed — the caller leaves the previous level untouched in that
    case. The decoder is reused across frames so it keeps Opus's
    inter-frame state, matching how the device encoded the stream.
    """
    global _level_decoder
    if not frame:
        return None
    try:
        if _level_decoder is None:
            import opuslib  # type: ignore[import-not-found]

            from .stt.audio_utils import (
                DEVICE_CHANNELS,
                DEVICE_SAMPLE_RATE,
            )

            _level_decoder = opuslib.Decoder(DEVICE_SAMPLE_RATE, DEVICE_CHANNELS)
        from .stt.audio_utils import SAMPLES_PER_FRAME

        pcm = _level_decoder.decode(frame, SAMPLES_PER_FRAME)
    except Exception:
        # opuslib missing, decode error, or a transient hiccup — the
        # meter is cosmetic, so never let it disturb the capture path.
        return None
    return _rms_from_pcm16(pcm)


def _rms_from_pcm16(pcm: bytes) -> float:
    """RMS of signed-16 little-endian PCM, normalised to 0.0-1.0."""
    import array
    import math

    if not pcm:
        return 0.0
    samples = array.array("h")
    # An odd trailing byte would break frombytes; clamp to an even length.
    usable = len(pcm) - (len(pcm) % 2)
    if usable <= 0:
        return 0.0
    samples.frombytes(pcm[:usable])
    if not samples:
        return 0.0
    mean_sq = sum(s * s for s in samples) / len(samples)
    rms = math.sqrt(mean_sq) / _INT16_FULL_SCALE
    return min(1.0, rms)


def is_recording_session(session_id: str) -> bool:
    """Return ``True`` when the recording slot belongs to ``session_id``.

    Used by per-session disconnect cleanup paths to confirm they still
    own the recording before tearing it down. A stale handler whose
    session was replaced by a fresh reconnection (or by an MCP-driven
    ``listen()``) must not clear the active buffer.
    """
    return _recording_session_id == session_id


async def handle_audio_frame(data: bytes, session_id: str) -> None:
    """Process an incoming binary Opus frame from the device.

    When a recording slot is active (see :func:`start_recording`) AND
    the frame belongs to the recording's session, appends the frame
    to the in-memory buffer for later decoding by the STT
    orchestrator. Frames from a different session — typical during
    a connection swap, where the old WebSocket handler is still
    draining incoming bytes after :meth:`ESP32Connection.disconnect`
    has been called on the main task — are dropped so they cannot
    bleed into the new connection's capture buffer.

    Outside of an active recording the frame is logged at debug
    level and discarded; the device may emit audio on its own (e.g.
    after an autonomous wake-word detection) and the gateway has no
    STT pipeline running for those frames yet.
    """
    if _recording_session_id is None:
        logger.debug(
            "audio_frame session=%s bytes=%d (discarded — no active recording)",
            session_id,
            len(data),
        )
        return
    if _recording_session_id != session_id:
        # A different connection is sending audio while a recording
        # for this session is in flight. This happens when ESP32
        # reconnects: ``ESP32Manager._handler`` swaps in a new
        # ``ESP32Connection`` and marks the old one disconnected,
        # but the old socket's ``async for message in ws`` loop can
        # still drain a frame or two before the close lands. Letting
        # those into the buffer would corrupt the new session's
        # transcription, so drop them here.
        logger.debug(
            "audio_frame session=%s bytes=%d (discarded — does not match "
            "recording session=%s)",
            session_id,
            len(data),
            _recording_session_id,
        )
        return
    _recording_frames.append(data)
    # Phase F: update the live mic level for the dashboard meter. The
    # decode is best-effort — a failure leaves the previous level in
    # place rather than disturbing the capture buffer above.
    global _last_level
    level = _frame_rms_level(data)
    if level is not None:
        _last_level = level
    logger.debug(
        "audio_frame session=%s bytes=%d buffered (recording active)",
        session_id,
        len(data),
    )


async def push_opus_frames(
    esp32: ESP32Manager,
    frames: Iterable[bytes],
) -> int:
    """Push Opus frames to the connected ESP32.

    Returns the number of frames sent so the caller can report this to
    the MCP client. Raises :class:`ConnectionError` (via
    :meth:`ESP32Manager.send_audio_frame`) if the device disconnects
    mid-stream — the orchestrator turns that into a clean MCP error
    rather than letting it bubble up as a stack trace.
    """
    sent = 0
    for frame in frames:
        await esp32.send_audio_frame(frame)
        sent += 1
    return sent
