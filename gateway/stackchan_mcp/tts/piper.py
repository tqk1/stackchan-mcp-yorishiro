"""Piper engine — in-process English (and multilingual) neural TTS.

Piper (https://github.com/OHF-Voice/piper1-gpl, originally
rhasspy/piper) is a fast, fully-offline neural TTS that runs on CPU via
onnxruntime. Unlike VOICEVOX — which is a heavyweight Qt application run
as a separate HTTP process, partly for LGPL license isolation — Piper is
a small Python package that we import and run *in process*. That is the
right fit here for three reasons:

* **Latency.** The gateway's design principle is "latency over quality".
  Loading the ONNX model once and running inference in-process avoids the
  per-call HTTP round-trip (and the per-call model reload that a bare CLI
  invocation would incur). The model stays resident on the engine
  instance across calls.
* **Deployment.** The target user is a non-developer on Windows with no
  GPU. ``pip install stackchan-mcp[tts-piper]`` pulls everything needed
  (the current ``piper-tts`` ships a ``win_amd64`` wheel); there is no
  second server process for them to start and babysit.

Licensing note: the maintained ``piper-tts`` package (OHF-Voice /
``piper1-gpl``, resolved here as 1.6.x) is GPL-3.0 — the original
``rhasspy/piper`` was MIT, but that line is unmaintained. It is declared
as an *optional, user-installed* extra and imported only at runtime, so
the gateway itself stays MIT (nothing GPL is vendored or distributed
here); the GPL applies to the user's own installed copy. This module
therefore never keeps Piper behind an HTTP boundary for license reasons —
running in-process is purely the latency choice above.

The engine returns 16 kHz mono signed-16-bit-LE PCM, matching the
:class:`~stackchan_mcp.tts.base.TTSEngine` contract; Piper's native
sample rate (usually 22.05 kHz) is resampled down via
:func:`~stackchan_mcp.tts.audio_utils.resample_pcm16_linear`, exactly as
the VOICEVOX engine does. Opus encoding and WebSocket delivery remain the
orchestrator's job.

Configuration:

    ``STACKCHAN_PIPER_MODEL``
        Filesystem path to a Piper ``.onnx`` voice model (its
        ``<model>.onnx.json`` config is picked up automatically by Piper
        from the same directory). Required unless ``model_path`` is
        passed to :class:`PiperEngine` directly. Download voices from
        https://huggingface.co/rhasspy/piper-voices (e.g.
        ``en_US-lessac-medium``).

    ``STACKCHAN_TTS_DEFAULT_VOICE``
        Set to ``piper`` to make the gateway speak English by default
        without passing ``voice="piper"`` on every ``say`` call (handled
        in :mod:`stackchan_mcp.tts.orchestrator`). The built-in default
        stays ``voicevox`` so existing Japanese operation is unchanged.

    ``STACKCHAN_PIPER_TIMEOUT_S``
        Upper bound, in seconds, on one synthesis call (including the
        one-off model load). Defaults to :data:`DEFAULT_TIMEOUT_S`.
        Raise it only on very slow hardware; the point of the bound is
        to fail with a readable error before the MCP client's own
        timeout turns the call into an unexplained stall.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any, Callable

from .audio_utils import DEVICE_SAMPLE_RATE, resample_pcm16_linear
from .base import TTSEngine

logger = logging.getLogger(__name__)


#: Environment variable naming the Piper ``.onnx`` model file to load.
PIPER_MODEL_ENV = "STACKCHAN_PIPER_MODEL"

#: Environment variable overriding :data:`DEFAULT_TIMEOUT_S`.
PIPER_TIMEOUT_ENV = "STACKCHAN_PIPER_TIMEOUT_S"

#: Upper bound, in seconds, on one ``synthesize`` call. Piper runs far
#: faster than real time even on modest CPUs, and the model load is a
#: couple of seconds, so 30 s is generous for legitimate work. It sits
#: below the 60 s timeout MCP clients typically apply, which matters:
#: without a bound of our own, a wedged native import inside the worker
#: thread reaches the caller as a silent stall with nothing in the log.
DEFAULT_TIMEOUT_S = 30.0


def _resolve_timeout_s() -> float:
    """Return the synthesis timeout, falling back on a malformed value.

    A bad :data:`PIPER_TIMEOUT_ENV` must not make ``say`` unusable, so a
    non-numeric or non-positive setting is logged and ignored rather
    than raised.
    """
    raw = os.getenv(PIPER_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Ignoring %s=%r: not a number; using %.0fs",
            PIPER_TIMEOUT_ENV,
            raw,
            DEFAULT_TIMEOUT_S,
        )
        return DEFAULT_TIMEOUT_S
    if value <= 0:
        logger.warning(
            "Ignoring %s=%r: must be positive; using %.0fs",
            PIPER_TIMEOUT_ENV,
            raw,
            DEFAULT_TIMEOUT_S,
        )
        return DEFAULT_TIMEOUT_S
    return value


def _default_voice_loader(model_path: str) -> Any:
    """Load a :class:`piper.PiperVoice` from ``model_path``.

    Imported lazily so this module stays importable without the
    ``piper-tts`` extra installed (mirroring how ``voicevox.py`` imports
    ``httpx`` only inside ``synthesize``). Tests inject a fake loader via
    :class:`PiperEngine`'s ``voice_loader`` argument, so this real loader
    is never exercised by the unit suite.

    Both the modern (``piper>=1.3`` / OHF-Voice) and the legacy
    (``rhasspy/piper``) packages expose ``PiperVoice`` — the former at the
    top level, the latter under ``piper.voice`` — so we try both import
    paths.
    """
    try:
        from piper import PiperVoice  # type: ignore[import-not-found]
    except ImportError:
        from piper.voice import PiperVoice  # type: ignore[import-not-found]
    return PiperVoice.load(model_path)


def _voice_to_pcm(voice: Any, text: str) -> tuple[int, bytes]:
    """Run synthesis on a loaded Piper voice, returning ``(rate, pcm)``.

    ``pcm`` is signed 16-bit little-endian mono at the voice's native
    sample rate; the caller resamples to :data:`DEVICE_SAMPLE_RATE`.

    Piper's Python API changed shape between releases, so we support both
    generations:

    * **Legacy** (``rhasspy/piper``): ``synthesize_stream_raw(text)``
      yields raw int16 byte chunks and the sample rate lives on
      ``voice.config.sample_rate``.
    * **Modern** (``piper>=1.3``): ``synthesize(text)`` yields
      ``AudioChunk`` objects carrying ``audio_int16_bytes`` and
      ``sample_rate``.

    The pinned extra installs the modern package, so that is the tested
    real path; the legacy branch is a safety net for users on an older
    install.
    """
    # Legacy raw-stream API is unambiguous — detect it first.
    if hasattr(voice, "synthesize_stream_raw"):
        sample_rate = int(voice.config.sample_rate)
        pcm = b"".join(voice.synthesize_stream_raw(text))
        return sample_rate, pcm

    # Modern generator API: collect int16 bytes across chunks.
    sample_rate: int | None = None
    buf = bytearray()
    for chunk in voice.synthesize(text):
        buf += chunk.audio_int16_bytes
        if sample_rate is None:
            sample_rate = int(chunk.sample_rate)

    if sample_rate is None:
        raise RuntimeError("Piper produced no audio for the given text")
    return sample_rate, bytes(buf)


class PiperEngine(TTSEngine):
    """Synthesise text with an in-process Piper voice model.

    Setup (recommended)::

        pip install stackchan-mcp[tts-piper]
        # download a voice, e.g. en_US-lessac-medium.onnx (+ .onnx.json)
        export STACKCHAN_PIPER_MODEL=/path/to/en_US-lessac-medium.onnx

    The voice model is loaded once, on the first ``synthesize`` call, and
    cached on the instance. Inference is CPU-bound and blocking, so it is
    run in a worker thread via :func:`asyncio.to_thread` to keep the
    gateway's event loop responsive.
    """

    name = "piper"

    def __init__(
        self,
        model_path: str | None = None,
        *,
        voice_loader: Callable[[str], Any] | None = None,
    ) -> None:
        """Construct a Piper engine.

        Args:
            model_path: Path to a Piper ``.onnx`` voice model. Falls back
                to the ``STACKCHAN_PIPER_MODEL`` environment variable.
                Resolution is deferred to the first ``synthesize`` call,
                so an engine can be constructed (and registered) before a
                model is configured.
            voice_loader: Callable that loads a voice object from a model
                path. Defaults to :func:`_default_voice_loader` (the real
                Piper loader). Tests inject a fake loader that returns a
                stub voice, so the unit suite never touches ``piper-tts``
                or a real ``.onnx`` file — the same dependency-injection
                seam VOICEVOX uses with ``transport``.
        """
        self._model_path = model_path or os.getenv(PIPER_MODEL_ENV)
        self._voice_loader = voice_loader or _default_voice_loader
        self._voice: Any = None
        # Guards the lazy load so two concurrent first calls (each on its
        # own to_thread worker) don't both load the model.
        self._load_lock = threading.Lock()

    @property
    def model_path(self) -> str | None:
        """Configured model path (or ``None`` if not yet set). Diagnostics."""
        return self._model_path

    def _get_voice(self) -> Any:
        """Return the loaded voice, loading and caching it on first use."""
        if self._voice is not None:
            return self._voice
        with self._load_lock:
            if self._voice is not None:  # another thread won the race
                return self._voice
            if not self._model_path:
                raise RuntimeError(
                    "Piper model path is not configured. Set the "
                    f"{PIPER_MODEL_ENV} environment variable to a Piper "
                    ".onnx voice model, or pass model_path=. Download "
                    "voices from https://huggingface.co/rhasspy/piper-voices."
                )
            # Logged *before* the call, not just after it. The loader
            # performs the first real ``import piper`` — which drags in
            # onnxruntime and espeak-ng native libraries — and then reads
            # the model. If any of that stalls, a success-only log line
            # would leave no trace of how far synthesis got.
            logger.info("Loading Piper voice model: %s", self._model_path)
            self._voice = self._voice_loader(self._model_path)
            logger.info("Loaded Piper voice model: %s", self._model_path)
            return self._voice

    def warmup(self) -> None:
        """Load the voice model now, on the calling thread.

        Overrides the no-op :meth:`~stackchan_mcp.tts.base.TTSEngine.warmup`
        so the gateway pays Piper's first-use cost at startup. Without
        this the model loads inside the first ``say`` — on a worker
        thread, mid-conversation — and a native import that is merely
        slow is indistinguishable from one that is stuck.

        Does nothing when no model is configured: the engine registers
        whenever the ``piper`` package is importable, so a user running
        VOICEVOX only would otherwise see a startup failure for an
        engine they never asked for. ``synthesize`` still reports the
        missing path when it is actually called.
        """
        if not self._model_path:
            return
        self._get_voice()

    def _blocking_synthesize(self, text: str) -> tuple[int, bytes]:
        """Load (if needed) and synthesise. Runs in a worker thread."""
        voice = self._get_voice()
        return _voice_to_pcm(voice, text)

    async def synthesize(self, text: str, **opts: Any) -> bytes:
        """Synthesise ``text`` into 16 kHz mono PCM (signed 16-bit LE).

        Piper selects its voice via the loaded model file, so the
        ``speaker_id`` / ``reference_audio`` opts (meaningful for VOICEVOX
        and Irodori respectively) are ignored here — per the
        :class:`~stackchan_mcp.tts.base.TTSEngine` contract that engines
        ignore options they do not support rather than raise.
        """
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Piper synthesize: 'text' must be a non-empty string")

        timeout_s = _resolve_timeout_s()
        try:
            sample_rate, pcm = await asyncio.wait_for(
                asyncio.to_thread(self._blocking_synthesize, text),
                timeout=timeout_s,
            )
        except TimeoutError as exc:
            # ``asyncio.to_thread`` cannot be cancelled, so the worker
            # thread keeps running (and keeps holding ``_load_lock`` if
            # it stalled inside the model load). That is deliberate:
            # there is no safe way to kill a thread stuck in a native
            # call, and returning a readable error beats leaving the
            # caller to hit its own timeout with nothing to go on. A
            # repeat call will time out the same way until the stuck
            # load finishes or the gateway restarts.
            raise RuntimeError(
                f"Piper synthesis timed out after {timeout_s:.0f}s "
                f"(model={self._model_path!r}). If the log shows "
                f"'Loading Piper voice model' without a matching "
                f"'Loaded', the model load itself is stuck — check the "
                f"model file and that piper's native dependencies "
                f"(onnxruntime, espeak-ng) import on this machine. "
                f"Raise {PIPER_TIMEOUT_ENV} if the hardware is simply slow."
            ) from exc

        if sample_rate != DEVICE_SAMPLE_RATE:
            pcm = resample_pcm16_linear(pcm, sample_rate, DEVICE_SAMPLE_RATE)

        logger.info(
            "Piper synthesised %d bytes PCM (16 kHz mono) from %d Hz for text=%r",
            len(pcm),
            sample_rate,
            text[:60],
        )
        return pcm
