"""Local-LLM fast path for voice turns (Phase C response routing).

yorishiro fork specific module (not intended for upstream PR).

Hermes runs on an external LLM and one voice turn costs ~2.6-3.4 s
before TTS even starts. Short, simple utterances (greetings, quick
questions, command-style phrases) do not need the full agent, so the
voice bridge (:mod:`stackchan_mcp.hermes_bridge`) can route them to a
small model served by a local Ollama instance instead.

The routing decision itself (:func:`decide_route`) is a rule-based
pure function so it can be unit-tested in isolation. Anything that
looks like it needs tools, memory or deliberation stays on Hermes;
the local model only ever answers from the prompt, so the rules are
deliberately conservative.

Environment variables:

- ``STACKCHAN_LOCAL_LLM_MODEL`` — Ollama model name (e.g.
  ``hf.co/LiquidAI/LFM2.5-1.2B-JP-202606-GGUF:Q4_K_M``). Routing is
  **opt-in**: when unset/empty this module is inert and every turn
  goes to Hermes, identical to the pre-routing behaviour (same
  opt-in pattern as ``STACKCHAN_VOICE_DUMP_DIR``).
- ``STACKCHAN_LOCAL_LLM_URL`` — Ollama base URL. Defaults to
  ``http://127.0.0.1:11434``.
- ``STACKCHAN_LOCAL_LLM_TIMEOUT_S`` — per-call timeout in seconds.
  Defaults to 10. Kept short on purpose: past this the local path
  has lost its latency advantage and the caller falls back to Hermes.
- ``STACKCHAN_LOCAL_LLM_KEEP_ALIVE`` — Ollama ``keep_alive`` value.
  Defaults to ``30m`` so the model stays resident between turns and
  cold-start (~3 s for the 1.2B model) is paid at most once per idle
  window.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import unicodedata

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_LOCAL_LLM_URL = "http://127.0.0.1:11434"
DEFAULT_LOCAL_LLM_TIMEOUT_S = 10.0
DEFAULT_LOCAL_LLM_KEEP_ALIVE = "30m"

ROUTE_LOCAL = "local"
ROUTE_HERMES = "hermes"

#: Utterances longer than this (after NFKC normalisation) are assumed
#: to carry real conversational content and go to Hermes. 30 chars of
#: Japanese comfortably covers greetings, one-clause questions and
#: command-style phrases ("電気をつけて") while excluding multi-clause
#: requests.
LOCAL_MAX_CHARS = 30

#: Substrings that signal the turn needs tools, memory, fresh facts or
#: deliberation — none of which the local model has. Matching any of
#: these forces the Hermes route regardless of length.
HERMES_MARKERS = (
    # needs web / fresh facts
    "調べ",
    "検索",
    "ニュース",
    "天気",
    # needs the agent's memory / schedule / side effects
    "予定",
    "スケジュール",
    "メール",
    "リマインド",
    "覚えて",
    "思い出して",
    "メモ",
    "リスト",
    # needs deliberation
    "なぜ",
    "どうして",
    "どう思",
    "説明して",
    "詳しく",
    # request-shaped utterances ("〜して" / "〜しといて" / "〜お願い")
    # imply an action, and actions need tools. This is deliberately
    # broad — false positives (「はじめまして」) just take the slower
    # Hermes path, while a false negative makes the tool-less local
    # model fake completed actions (observed live: STT mangled
    # 「メモして」 into 「埋めまして」, slipped past the specific
    # markers, and the local model claimed the memo was saved).
    "して",
    "といて",
    "ちょうだい",
    "頂戴",
    "お願い",
    # needs SwitchBot / device tools (home-appliance control lives on
    # Hermes via the gateway's switchbot_* MCP tools; the local model
    # cannot call tools, so command-style utterances must not short-cut)
    "つけて",
    "点けて",
    "消して",
    "切って",
    "電気",
    "照明",
    "エアコン",
    "テレビ",
    "温度",
    "湿度",
    "スイッチ",
    "デバイス",
)

#: Reasoning models (e.g. qwen3) wrap chain-of-thought in <think>
#: tags; spoken output must never include them.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

_WEEKDAYS_JA = "月火水木金土日"

#: Substrings that mean the user is actually asking about the date or
#: weekday. The date context is injected into the local prompt **only**
#: when one of these matches the transcript — otherwise the 1.2B model
#: is never told today's date, so it cannot blurt it out on vague turns
#: (observed live: 「うっ」 → "…今日は2026年6月13日よ。…"). Matched after
#: NFKC normalisation, so full-width / half-width variants fold together.
DATE_QUERY_MARKERS = (
    "何日",
    "なんにち",
    "日付",
    "日にち",
    "何曜",
    "なん曜",
    "曜日",
    "今日",
    "きょう",
    "本日",
    "date",
    "today",
)


def _env_float(name: str, default: float) -> float:
    """A numeric env var in seconds, warning and falling back on garbage.

    A non-numeric value (a config typo) would otherwise raise on every
    turn and silently push the whole conversation onto the slow Hermes
    path; warn once-per-turn and fall back so the misconfiguration is
    visible in the logs instead.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("local LLM: invalid %s=%r; using %s", name, raw, default)
        return default


def is_enabled() -> bool:
    """True when local routing is opted in via STACKCHAN_LOCAL_LLM_MODEL."""
    return bool(os.getenv("STACKCHAN_LOCAL_LLM_MODEL", "").strip())


def decide_route(text: str) -> str:
    """Classify one transcript: :data:`ROUTE_LOCAL` or :data:`ROUTE_HERMES`.

    Pure function over the STT text — no I/O, no env lookups — so the
    routing policy is unit-testable on its own. Policy:

    1. Empty / whitespace-only → Hermes (defensive; the voice bridge
       already drops empty transcripts before routing).
    2. Any :data:`HERMES_MARKERS` substring → Hermes.
    3. At most :data:`LOCAL_MAX_CHARS` chars → local.
    4. Otherwise → Hermes.
    """
    normalized = unicodedata.normalize("NFKC", text).strip()
    if not normalized:
        return ROUTE_HERMES
    if any(marker in normalized for marker in HERMES_MARKERS):
        return ROUTE_HERMES
    if len(normalized) <= LOCAL_MAX_CHARS:
        return ROUTE_LOCAL
    return ROUTE_HERMES


#: The local model cannot call tools. STT mis-transcriptions can strip
#: the marker words that would have routed a tool request to Hermes
#: (e.g. 「メモして」→「埋めまして」), and without this line the model
#: happily claims to have saved memos or run searches it cannot run.
#: Asking back instead gives the user a natural retry, and the retried
#: utterance usually transcribes well enough to route to Hermes.
LOCAL_NO_TOOLS_LINE = (
    "重要: あなたは道具が使えないので、実行・保存・記録・調査・操作は一切できません。"
    "そういう依頼や、意味のよくわからない発話が来たら、内容をでっち上げず、"
    "「ごめん、うまく聞き取れなかったみたい。もう一度言ってもらえる？」とだけ答えてください。"
)


def _is_date_query(text: str) -> bool:
    """True when the transcript is asking about today's date / weekday.

    Pure keyword match over the NFKC-normalised text. Drives whether
    :func:`_today_line` is injected: a small local model that is never
    told today's date cannot volunteer it on a vague turn, so the date
    context is opt-in per turn rather than always-on.
    """
    normalized = unicodedata.normalize("NFKC", text).lower()
    return any(marker in normalized for marker in DATE_QUERY_MARKERS)


def _today_line(now: datetime.datetime | None = None) -> str:
    """Date context for the system prompt.

    A small local model has no notion of "today". Injected **only** when
    the transcript is a date/weekday question (see :func:`_is_date_query`),
    so the assertive phrasing is safe here — the user just asked.
    """
    if now is None:
        now = datetime.datetime.now()
    weekday = _WEEKDAYS_JA[now.weekday()]
    return (
        f"（参考情報：今日は{now.year}年{now.month}月{now.day}日"
        f"({weekday}曜日)です。）"
    )


async def ask_local(text: str, *, system_prompt: str) -> str:
    """Send one user turn to the local Ollama model, return the reply text.

    Mirrors :func:`stackchan_mcp.hermes_bridge.ask_hermes` in shape and
    error discipline (RuntimeError on any non-usable response) so the
    caller can treat both paths uniformly. ``system_prompt`` is passed
    in by the caller so the voice-style constraints stay single-sourced
    in the bridge module.
    """
    model = os.getenv("STACKCHAN_LOCAL_LLM_MODEL", "").strip()
    if not model:
        raise RuntimeError("STACKCHAN_LOCAL_LLM_MODEL is not set")
    base_url = os.getenv(
        "STACKCHAN_LOCAL_LLM_URL", DEFAULT_LOCAL_LLM_URL
    ).rstrip("/")
    timeout_s = _env_float(
        "STACKCHAN_LOCAL_LLM_TIMEOUT_S", DEFAULT_LOCAL_LLM_TIMEOUT_S
    )
    keep_alive = (
        os.getenv("STACKCHAN_LOCAL_LLM_KEEP_ALIVE", "")
        or DEFAULT_LOCAL_LLM_KEEP_ALIVE
    )

    # Inject today's date only when the user actually asks about it.
    # The 1.2B model cannot reliably keep "don't mention this unless
    # asked" context, so the only safe guard is to withhold the fact
    # entirely on non-date turns (observed live: 「うっ」 → unsolicited
    # "今日は2026年6月13日よ。").
    date_line = _today_line() if _is_date_query(text) else ""
    system_content = system_prompt + date_line + LOCAL_NO_TOOLS_LINE

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": system_content,
            },
            {"role": "user", "content": text},
        ],
        "stream": False,
        # Keep the model resident between turns; reloading the weights
        # would cost more than the Hermes round-trip we are avoiding.
        "keep_alive": keep_alive,
    }

    timeout = aiohttp.ClientTimeout(total=timeout_s)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(f"{base_url}/api/chat", json=payload) as resp:
            body = await resp.text()
            if resp.status != 200:
                logger.error(
                    "local LLM status=%d body=%s", resp.status, body[:500]
                )
                raise RuntimeError(
                    f"local LLM returned status={resp.status}"
                )
    data = json.loads(body)
    message = data.get("message")
    reply = message.get("content") if isinstance(message, dict) else None
    if not isinstance(reply, str):
        logger.error("local LLM response missing message.content: %s", body[:500])
        raise RuntimeError("local LLM response missing message.content")
    reply = _THINK_RE.sub("", reply).strip()
    if not reply:
        raise RuntimeError("local LLM returned an empty reply")
    return reply
