"""Voice-turn bridge: device-driven capture → STT → Hermes Agent → TTS.

yorishiro fork specific module (not intended for upstream PR).

The firmware records while the user holds a device-side trigger (LCD
tap) and the gateway's :mod:`audio_input_hook` POSTs the finished
capture as Ogg/Opus to ``STACKCHAN_AUDIO_HOOK_URL``. Pointing that URL
at this gateway's own capture server (``http://127.0.0.1:8766/voice_turn``)
closes the conversation loop in-process:

    tap → record → POST /voice_turn → STT (faster-whisper)
        → Hermes Agent (OpenAI-compatible API server)
        → TTS (say() pipeline) → device speaker

Environment variables:

- ``HERMES_API_URL`` — base URL of the Hermes API server adapter.
  Defaults to ``http://127.0.0.1:8642``.
- ``HERMES_API_KEY`` — bearer token for the Hermes API server. Optional;
  when set, requests also carry ``X-Hermes-Session-Id`` so Hermes keeps
  one persistent conversation (session continuity requires the key).
- ``HERMES_SESSION_ID`` — session id used with the key above.
  Defaults to ``stackchan-voice``.
- ``HERMES_VOICE_SYSTEM_PROMPT`` — overrides the default system prompt
  that keeps spoken replies short.
- ``STACKCHAN_AUDIO_HOOK_TOKEN`` — shared bearer token; when set, the
  ``/voice_turn`` endpoint rejects requests without it (the sender side
  in :mod:`audio_input_hook` attaches the same token).
"""

from __future__ import annotations

import asyncio
import hmac
import io
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web

if TYPE_CHECKING:
    from .gateway import Gateway

logger = logging.getLogger(__name__)

DEFAULT_HERMES_API_URL = "http://127.0.0.1:8642"
DEFAULT_HERMES_SESSION_ID = "stackchan-voice"

#: Spoken replies must stay short — they are synthesised and played on
#: a 1 W speaker, and long monologues kill the conversation rhythm.
DEFAULT_VOICE_SYSTEM_PROMPT = (
    "あなたは小型ロボット「スタックチャン」として音声で会話しています。"
    "ユーザーの発話は音声認識の結果なので、多少の誤認識は文脈から補ってください。"
    "返答は話し言葉で短く、1〜3文にまとめてください。記号や箇条書きは使わないでください。"
)

#: Hard ceiling for one Hermes turn. The agent may run tools internally;
#: beyond this the voice interaction is dead anyway.
HERMES_TIMEOUT_S = 120.0


def _ogg_opus_to_pcm16k(data: bytes) -> bytes:
    """Decode an Ogg/Opus capture to 16 kHz mono s16 PCM via PyAV."""
    import av
    from av.audio.resampler import AudioResampler

    out = bytearray()
    resampler = AudioResampler(format="s16", layout="mono", rate=16000)
    with av.open(io.BytesIO(data)) as container:
        for frame in container.decode(audio=0):
            for rframe in resampler.resample(frame):
                out.extend(bytes(rframe.planes[0])[: rframe.samples * 2])
        # Flush the resampler's internal FIFO.
        for rframe in resampler.resample(None):
            out.extend(bytes(rframe.planes[0])[: rframe.samples * 2])
    return bytes(out)


async def ask_hermes(text: str) -> str:
    """Send one user turn to the Hermes API server, return the reply text."""
    base_url = os.getenv("HERMES_API_URL", DEFAULT_HERMES_API_URL).rstrip("/")
    api_key = os.getenv("HERMES_API_KEY", "")
    system_prompt = os.getenv(
        "HERMES_VOICE_SYSTEM_PROMPT", DEFAULT_VOICE_SYSTEM_PROMPT
    )

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        # Session continuity is gated on API-key auth by the Hermes API
        # server; without the key we stay stateless.
        headers["X-Hermes-Session-Id"] = os.getenv(
            "HERMES_SESSION_ID", DEFAULT_HERMES_SESSION_ID
        )

    payload = {
        "model": "hermes-agent",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
    }

    timeout = aiohttp.ClientTimeout(total=HERMES_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            f"{base_url}/v1/chat/completions", json=payload, headers=headers
        ) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise RuntimeError(
                    f"Hermes API returned status={resp.status}: {body[:300]}"
                )
    data = json.loads(body)
    try:
        reply = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            f"Hermes API response missing choices: {body[:300]}"
        ) from exc
    if not isinstance(reply, str) or not reply.strip():
        raise RuntimeError("Hermes API returned an empty reply")
    return reply.strip()


def _check_token(request: web.Request) -> bool:
    """Verify the shared audio-hook bearer token, if one is configured."""
    expected = os.getenv("STACKCHAN_AUDIO_HOOK_TOKEN", "")
    if not expected:
        return True
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    return hmac.compare_digest(auth[len("Bearer "):], expected)


async def handle_voice_turn(request: web.Request) -> web.Response:
    """POST /voice_turn — run one full voice conversation turn."""
    # Lazy imports keep capture-only deployments free of the stt/tts
    # extras (same pattern as the /pcm handler).
    from .capture_server import GATEWAY_KEY
    from .stt import get_registry as get_stt_registry
    from .stt.orchestrator import DEFAULT_ENGINE as DEFAULT_STT_ENGINE
    from .tts.orchestrator import synthesize_and_send

    if not _check_token(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    gateway: "Gateway | None" = request.app.get(GATEWAY_KEY)  # type: ignore[assignment]
    if gateway is None:
        return web.json_response(
            {"ok": False, "error": "gateway not attached"}, status=503
        )

    session_id = request.headers.get("X-StackChan-Session", "")
    ogg = await request.read()
    if not ogg:
        return web.json_response({"ok": False, "error": "empty body"}, status=400)

    t0 = time.monotonic()
    try:
        pcm = await asyncio.to_thread(_ogg_opus_to_pcm16k, ogg)
    except Exception as exc:
        logger.exception("voice_turn: Ogg/Opus decode failed")
        return web.json_response(
            {"ok": False, "error": f"decode failed: {exc}"}, status=400
        )
    t_decode = time.monotonic()

    engine = get_stt_registry().get(DEFAULT_STT_ENGINE)
    if engine is None:
        return web.json_response(
            {
                "ok": False,
                "error": (
                    f"STT engine '{DEFAULT_STT_ENGINE}' not registered — "
                    "install stackchan-mcp[stt-faster-whisper]"
                ),
            },
            status=503,
        )
    stt_result: dict[str, Any] = await engine.transcribe(pcm, language="ja")
    transcript = stt_result.get("text", "").strip()
    t_stt = time.monotonic()

    if not transcript:
        logger.info("voice_turn: empty transcript (noise?), session=%s", session_id)
        return web.json_response(
            {"ok": False, "reason": "empty transcript", "session_id": session_id}
        )

    logger.info("voice_turn: transcript=%r session=%s", transcript[:120], session_id)
    try:
        reply = await ask_hermes(transcript)
    except Exception as exc:
        logger.exception("voice_turn: Hermes call failed")
        return web.json_response(
            {"ok": False, "error": f"hermes failed: {exc}", "transcript": transcript},
            status=502,
        )
    t_hermes = time.monotonic()

    logger.info("voice_turn: reply=%r session=%s", reply[:120], session_id)
    try:
        tts_result = await synthesize_and_send({"text": reply}, gateway=gateway)
    except Exception as exc:
        logger.exception("voice_turn: TTS failed")
        return web.json_response(
            {
                "ok": False,
                "error": f"tts failed: {exc}",
                "transcript": transcript,
                "reply": reply,
            },
            status=502,
        )
    t_done = time.monotonic()

    timings_ms = {
        "decode": int((t_decode - t0) * 1000),
        "stt": int((t_stt - t_decode) * 1000),
        "hermes": int((t_hermes - t_stt) * 1000),
        "tts": int((t_done - t_hermes) * 1000),
        "total": int((t_done - t0) * 1000),
    }
    logger.info("voice_turn: done session=%s timings_ms=%s", session_id, timings_ms)
    return web.json_response(
        {
            "ok": True,
            "session_id": session_id,
            "transcript": transcript,
            "reply": reply,
            "tts": tts_result,
            "timings_ms": timings_ms,
        }
    )
