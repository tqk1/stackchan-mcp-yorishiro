"""Web search tool for the Hermes agent (Phase D).

yorishiro fork specific module (not intended for upstream PR).

Hermes drives the gateway over the Streamable HTTP MCP server, so a
gateway-local ``web_search`` tool gives the agent internet lookup
without a new service or Hermes-side registration — the same pattern
as the SwitchBot tools (Phase C).

Two backends, tried in order:

1. **Tavily** (https://tavily.com) — an LLM-oriented search API with
   a generous free tier. Used when ``TAVILY_API_KEY`` is set.
2. **DuckDuckGo** via the ``ddgs`` package — keyless fallback when
   Tavily is unconfigured or a Tavily call fails. Install with the
   ``search`` extra: ``stackchan-mcp[search]``.

Environment variables:

- ``TAVILY_API_KEY`` — Tavily API key. Optional; without it every
  search goes straight to DuckDuckGo.
- ``TAVILY_API_URL`` — API base URL override (tests / proxies).
  Defaults to ``https://api.tavily.com``.

Both backends normalise to the same shape::

    {"ok": True, "source": "tavily" | "ddgs", "query": ...,
     "answer": str | None, "results": [{"title", "url", "snippet"}]}

``answer`` is Tavily's LLM-composed summary (None for DuckDuckGo).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_TAVILY_API_URL = "https://api.tavily.com"

#: One search round-trip; the voice loop is dead long before this.
SEARCH_TIMEOUT_S = 15.0

DEFAULT_MAX_RESULTS = 5
MAX_RESULTS_CAP = 10

#: MCP tool names backed by this module (kept in sync with the HTTP
#: daemon's BYPASS_TOOLS — these never touch the ESP32).
TOOL_NAMES = frozenset({"web_search"})


def is_tavily_configured() -> bool:
    """True when TAVILY_API_KEY is set."""
    return bool(os.getenv("TAVILY_API_KEY", "").strip())


def _clamp_max_results(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_RESULTS
    return min(max(n, 1), MAX_RESULTS_CAP)


async def _search_tavily(query: str, max_results: int) -> dict[str, Any]:
    api_key = os.getenv("TAVILY_API_KEY", "").strip()
    base_url = os.getenv("TAVILY_API_URL", DEFAULT_TAVILY_API_URL).rstrip("/")
    payload = {
        "query": query,
        "max_results": max_results,
        "include_answer": True,
        "search_depth": "basic",
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    timeout = aiohttp.ClientTimeout(total=SEARCH_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            f"{base_url}/search", json=payload, headers=headers
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning(
                    "web_search: Tavily status=%d body=%s", resp.status, body[:300]
                )
                raise RuntimeError(f"Tavily returned status={resp.status}")
            data = await resp.json()

    results = [
        {
            "title": str(item.get("title", "")),
            "url": str(item.get("url", "")),
            "snippet": str(item.get("content", ""))[:500],
        }
        for item in data.get("results", [])
        if isinstance(item, dict)
    ]
    answer = data.get("answer")
    return {
        "ok": True,
        "source": "tavily",
        "query": query,
        "answer": answer if isinstance(answer, str) and answer.strip() else None,
        "results": results,
    }


def _ddgs_text_sync(query: str, max_results: int) -> list[dict[str, Any]]:
    """Blocking DuckDuckGo lookup; run via asyncio.to_thread.

    The package was renamed duckduckgo_search → ddgs; accept either so
    an older preinstalled environment keeps working.
    """
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # type: ignore[no-redef]
        except ImportError as exc:
            raise RuntimeError(
                "DuckDuckGo backend unavailable — install "
                "stackchan-mcp[search] (ddgs package)"
            ) from exc
    with DDGS() as ddgs:
        return list(ddgs.text(query, region="jp-jp", max_results=max_results))


async def _search_ddgs(query: str, max_results: int) -> dict[str, Any]:
    raw = await asyncio.wait_for(
        asyncio.to_thread(_ddgs_text_sync, query, max_results),
        timeout=SEARCH_TIMEOUT_S,
    )
    results = [
        {
            "title": str(item.get("title", "")),
            "url": str(item.get("href", item.get("url", ""))),
            "snippet": str(item.get("body", ""))[:500],
        }
        for item in raw
        if isinstance(item, dict)
    ]
    return {
        "ok": True,
        "source": "ddgs",
        "query": query,
        "answer": None,
        "results": results,
    }


async def search(query: str, max_results: Any = None) -> dict[str, Any]:
    """Run one web search, preferring Tavily, falling back to DuckDuckGo.

    Raises ValueError on an empty query and RuntimeError when every
    available backend fails — mirroring the switchbot module so the
    dispatcher can map both onto a clean error JSON.
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("query is required")
    n = _clamp_max_results(
        max_results if max_results is not None else DEFAULT_MAX_RESULTS
    )

    if is_tavily_configured():
        try:
            return await _search_tavily(query, n)
        except Exception as exc:
            logger.warning(
                "web_search: Tavily failed (%s); falling back to DuckDuckGo", exc
            )

    try:
        return await _search_ddgs(query, n)
    except Exception as exc:
        logger.warning("web_search: DuckDuckGo failed: %s", exc)
        raise RuntimeError(f"web search failed: {exc}") from exc
