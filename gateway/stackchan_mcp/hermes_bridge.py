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
- ``STACKCHAN_LOCAL_LLM_MODEL`` (and friends) — opt-in fast path that
  routes short/simple utterances to a local Ollama model instead of
  Hermes; see :mod:`stackchan_mcp.local_llm`. Unset = every turn goes
  to Hermes, exactly as before.
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

from . import local_llm

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

#: Tool-routing guidance appended to the voice system prompt. The
#: Hermes agent also has built-in tools (terminal, ...) that are
#: approval-gated in this deployment and tempt the model into dead
#: ends or fake completions — observed live in the Phase D2 E2E: a
#: weather question went to `curl wttr.in` via the terminal tool
#: (blocked pending approval) instead of the MCP web_search tool, and
#: a memo request was reported "added" without any tool call at all.
HERMES_VOICE_TOOLS_LINE = (
    "調べ物・天気・ニュースは必ず MCP ツールの web_search を使ってください。"
    "メモやリストの保存は write_note（追記は append=true）、"
    "内容の確認は list_notes / read_note、家電操作は switchbot_* ツールを使ってください。"
    "terminal など他の手段は使わないでください。"
    "ツールを呼ばずに「やりました」「調べました」と報告することは禁止です。"
)

#: Hard ceiling for one Hermes turn. The agent may run tools internally;
#: beyond this the voice interaction is dead anyway.
HERMES_TIMEOUT_S = 120.0

#: Upper bound for one uploaded capture. Device-driven recordings are
#: capped at 30 s on the firmware side; Opus at 16 kHz mono runs well
#: under 4 KiB/s, so 2 MiB is an order of magnitude above any real
#: voice turn. The capture server disables aiohttp's global body cap
#: (``client_max_size=0`` for /pcm streaming), so this route enforces
#: its own.
MAX_OGG_BYTES = 2 * 1024 * 1024

#: Upper bound for the decoded PCM (decompression-bomb guard): 120 s of
#: 16 kHz mono s16 audio. A malicious Ogg can expand far beyond its
#: wire size; abort the decode loop once past this.
MAX_PCM_BYTES = 120 * 16000 * 2


def _ogg_opus_to_pcm16k(data: bytes) -> bytes:
    """Decode an Ogg/Opus capture to 16 kHz mono s16 PCM via PyAV.

    Raises ValueError if the decoded audio exceeds :data:`MAX_PCM_BYTES`.
    """
    import av
    from av.audio.resampler import AudioResampler

    out = bytearray()
    resampler = AudioResampler(format="s16", layout="mono", rate=16000)
    with av.open(io.BytesIO(data)) as container:
        for frame in container.decode(audio=0):
            for rframe in resampler.resample(frame):
                out.extend(bytes(rframe.planes[0])[: rframe.samples * 2])
            if len(out) > MAX_PCM_BYTES:
                raise ValueError(
                    f"decoded audio exceeds {MAX_PCM_BYTES} bytes PCM"
                )
        # Flush the resampler's internal FIFO.
        for rframe in resampler.resample(None):
            out.extend(bytes(rframe.planes[0])[: rframe.samples * 2])
    if len(out) > MAX_PCM_BYTES:
        raise ValueError(f"decoded audio exceeds {MAX_PCM_BYTES} bytes PCM")
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
            {
                "role": "system",
                "content": system_prompt + HERMES_VOICE_TOOLS_LINE,
            },
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
                # Log the upstream body server-side only; the HTTP
                # caller gets a generic message (no provider internals).
                logger.error(
                    "Hermes API status=%d body=%s", resp.status, body[:500]
                )
                raise RuntimeError(
                    f"Hermes API returned status={resp.status}"
                )
    data = json.loads(body)
    try:
        reply = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        logger.error("Hermes API response missing choices: %s", body[:500])
        raise RuntimeError("Hermes API response missing choices") from exc
    if not isinstance(reply, str) or not reply.strip():
        raise RuntimeError("Hermes API returned an empty reply")
    return reply.strip()


async def generate_reply(text: str) -> tuple[str, str]:
    """Produce the reply for one transcript, returning ``(reply, route)``.

    With local routing opted in (``STACKCHAN_LOCAL_LLM_MODEL`` set) and
    :func:`local_llm.decide_route` classifying the turn as short/simple,
    the local Ollama model answers; on any local failure (timeout,
    connection refused, bad response) the turn falls back to Hermes so
    routing can never kill a conversation. ``route`` is ``"local"`` or
    ``"hermes"``.
    """
    if (
        local_llm.is_enabled()
        and local_llm.decide_route(text) == local_llm.ROUTE_LOCAL
    ):
        system_prompt = os.getenv(
            "HERMES_VOICE_SYSTEM_PROMPT", DEFAULT_VOICE_SYSTEM_PROMPT
        )
        try:
            reply = await local_llm.ask_local(text, system_prompt=system_prompt)
            return reply, local_llm.ROUTE_LOCAL
        except Exception as exc:
            logger.warning(
                "voice_turn: local LLM failed (%s); falling back to Hermes", exc
            )
    return await ask_hermes(text), local_llm.ROUTE_HERMES


def _check_token(request: web.Request) -> bool:
    """Authorise a /voice_turn caller.

    With ``STACKCHAN_AUDIO_HOOK_TOKEN`` configured, require the matching
    bearer token. Without a token, fail closed for everything except
    loopback peers: the capture server binds non-loopback interfaces
    (the ESP32 POSTs /capture over the LAN), and this route invokes an
    agent — it must not be open to the whole LAN by default.
    """
    expected = os.getenv("STACKCHAN_AUDIO_HOOK_TOKEN", "")
    if not expected:
        return request.remote in ("127.0.0.1", "::1")
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

    # Phase E: stamp the interaction at the very start of the turn —
    # while the STT/Hermes round-trip is in flight neither tts_lock nor
    # the recording slot is held, so this timestamp is what keeps the
    # heartbeat from speaking into that gap.
    gateway.note_human_interaction()

    session_id = request.headers.get("X-StackChan-Session", "")
    if (request.content_length or 0) > MAX_OGG_BYTES:
        return web.json_response(
            {"ok": False, "error": "payload too large"}, status=413
        )
    # content_length can be absent/lied about (chunked transfer), so
    # also enforce the cap on the actual stream.
    ogg = await request.content.read(MAX_OGG_BYTES + 1)
    if len(ogg) > MAX_OGG_BYTES:
        return web.json_response(
            {"ok": False, "error": "payload too large"}, status=413
        )
    if not ogg:
        return web.json_response({"ok": False, "error": "empty body"}, status=400)

    # Opt-in capture dump for diagnosing mic quality (STT/VAD issues).
    dump_dir = os.getenv("STACKCHAN_VOICE_DUMP_DIR", "")
    if dump_dir:
        try:
            os.makedirs(dump_dir, exist_ok=True)
            dump_path = os.path.join(
                dump_dir, f"voice_turn_{int(time.time())}.ogg"
            )
            with open(dump_path, "wb") as fp:
                fp.write(ogg)
            logger.info("voice_turn: capture dumped to %s", dump_path)
        except OSError:
            logger.exception("voice_turn: capture dump failed")

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
        reply, route = await generate_reply(transcript)
    except Exception as exc:
        logger.exception("voice_turn: Hermes call failed")
        return web.json_response(
            {"ok": False, "error": f"hermes failed: {exc}", "transcript": transcript},
            status=502,
        )
    t_llm = time.monotonic()

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
        # "llm" covers whichever brain answered; "route" says which.
        "llm": int((t_llm - t_stt) * 1000),
        "tts": int((t_done - t_llm) * 1000),
        "total": int((t_done - t0) * 1000),
    }
    logger.info(
        "voice_turn: done session=%s route=%s timings_ms=%s",
        session_id,
        route,
        timings_ms,
    )
    return web.json_response(
        {
            "ok": True,
            "session_id": session_id,
            "transcript": transcript,
            "reply": reply,
            "route": route,
            "tts": tts_result,
            "timings_ms": timings_ms,
        }
    )
