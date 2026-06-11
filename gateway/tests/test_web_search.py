"""Tests for the Phase D web_search tool (Tavily + DuckDuckGo fallback)."""

from typing import Any

import pytest
from aiohttp import web

from stackchan_mcp import web_search


@pytest.fixture
def aiohttp_unused_port():
    """Helper: pick an unused TCP port via ephemeral bind."""
    import socket

    def _pick() -> int:
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]
        finally:
            sock.close()

    return _pick


async def _run_stub(
    routes: list[tuple[str, str, Any]], aiohttp_unused_port
) -> tuple[web.AppRunner, str]:
    app = web.Application()
    for method, path, handler in routes:
        app.router.add_route(method, path, handler)
    port = aiohttp_unused_port()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner, f"http://127.0.0.1:{port}"


def _configure_tavily(monkeypatch, base_url: str) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key")
    monkeypatch.setenv("TAVILY_API_URL", base_url)


def test_is_tavily_configured(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert not web_search.is_tavily_configured()
    monkeypatch.setenv("TAVILY_API_KEY", "   ")
    assert not web_search.is_tavily_configured()
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    assert web_search.is_tavily_configured()


def test_clamp_max_results():
    assert web_search._clamp_max_results(None) == 5
    assert web_search._clamp_max_results("junk") == 5
    assert web_search._clamp_max_results(0) == 1
    assert web_search._clamp_max_results(99) == 10
    assert web_search._clamp_max_results(3) == 3


@pytest.mark.asyncio
async def test_empty_query_raises():
    with pytest.raises(ValueError):
        await web_search.search("   ")


@pytest.mark.asyncio
async def test_tavily_success(monkeypatch, aiohttp_unused_port):
    seen: dict[str, Any] = {}

    async def handle(request: web.Request) -> web.Response:
        seen["auth"] = request.headers.get("Authorization", "")
        seen["payload"] = await request.json()
        return web.json_response(
            {
                "query": "stackchan",
                "answer": "StackChan is a palm-sized robot.",
                "results": [
                    {
                        "title": "StackChan",
                        "url": "https://example.com/sc",
                        "content": "A" * 600,
                        "score": 0.9,
                    }
                ],
            }
        )

    runner, base_url = await _run_stub(
        [("POST", "/search", handle)], aiohttp_unused_port
    )
    _configure_tavily(monkeypatch, base_url)
    try:
        result = await web_search.search("stackchan", max_results=3)
    finally:
        await runner.cleanup()

    assert seen["auth"] == "Bearer tvly-test-key"
    assert seen["payload"]["max_results"] == 3
    assert result["ok"] is True
    assert result["source"] == "tavily"
    assert result["answer"] == "StackChan is a palm-sized robot."
    assert result["results"][0]["url"] == "https://example.com/sc"
    assert len(result["results"][0]["snippet"]) == 500  # truncated


@pytest.mark.asyncio
async def test_tavily_failure_falls_back_to_ddgs(
    monkeypatch, aiohttp_unused_port
):
    async def handle(request: web.Request) -> web.Response:
        return web.Response(status=500, text="boom")

    runner, base_url = await _run_stub(
        [("POST", "/search", handle)], aiohttp_unused_port
    )
    _configure_tavily(monkeypatch, base_url)

    async def fake_ddgs(query, n):
        return {
            "ok": True,
            "source": "ddgs",
            "query": query,
            "answer": None,
            "results": [],
        }

    monkeypatch.setattr(web_search, "_search_ddgs", fake_ddgs)
    try:
        result = await web_search.search("stackchan")
    finally:
        await runner.cleanup()
    assert result["source"] == "ddgs"


@pytest.mark.asyncio
async def test_unconfigured_goes_straight_to_ddgs(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def sync_results(query, n):
        return [
            {"title": "t", "href": "https://example.com", "body": "b"},
        ]

    monkeypatch.setattr(web_search, "_ddgs_text_sync", sync_results)
    result = await web_search.search("stackchan")
    assert result["source"] == "ddgs"
    assert result["answer"] is None
    assert result["results"] == [
        {"title": "t", "url": "https://example.com", "snippet": "b"}
    ]


@pytest.mark.asyncio
async def test_all_backends_failing_raises(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def explode(query, n):
        raise RuntimeError("no backend")

    monkeypatch.setattr(web_search, "_ddgs_text_sync", explode)
    with pytest.raises(RuntimeError):
        await web_search.search("stackchan")
