"""Seam 1: the MCP `fetch` tool, called over Streamable HTTP the way Claude Code calls it,
against the local fake web and the fake Firecrawl."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from mcp.types import CallToolResult, TextContent

from scraper.api import create_app
from scraper.config import Config

from .fakefirecrawl import FakeFirecrawl, firecrawl_page
from .fakeweb import FakeWeb, page
from .mcpcaller import McpCaller
from .test_paid_fetcher import ARTICLE_MARKDOWN


@pytest.fixture
def mcp(config: Config) -> Iterator[McpCaller]:
    caller = McpCaller(create_app(config))
    caller.start()
    yield caller
    caller.stop()


def text(result: CallToolResult) -> str:
    """What the model reads: the tool result's text."""
    return "\n".join(c.text for c in result.content if isinstance(c, TextContent))


def data(result: CallToolResult) -> dict[str, Any]:
    assert result.structuredContent is not None
    return result.structuredContent


def test_fetch_tool_is_listed_with_its_arguments(mcp: McpCaller) -> None:
    (tool,) = [t for t in mcp.tools() if t.name == "fetch"]

    props = tool.inputSchema["properties"]
    assert set(props) == {"url", "max_age_seconds", "fetcher"}
    assert tool.inputSchema["required"] == ["url"]
    # The Fetchers a Caller may force are named, so the model knows the choices.
    assert "plain" in props["fetcher"]["description"]
    assert "paid" in props["fetcher"]["description"]
    assert tool.annotations is not None and tool.annotations.readOnlyHint is True


def test_fetch_returns_markdown_title_fetcher_verdict_and_confidence(
    mcp: McpCaller, web: FakeWeb
) -> None:
    url = web.serve("/article", page("article.html"))

    result = mcp.call("fetch", url=url)

    assert result.isError is False
    got = data(result)
    assert "Raspberry Pi 5 runs the" in got["markdown"]
    assert got["title"].startswith("How Raspberry Pi 5 Handles Sustained Workloads")
    assert got["url"] == url
    assert got["statusCode"] == 200
    assert got["fetcher"] == "plain"
    assert got["verdict"] == "accepted"
    assert got["confidence"] == "accepted"
    assert got["publishedDate"] == "2026-10-14"
    assert got["cachedAt"] is None
    # The model reads the same facts, then the page.
    shown = text(result)
    assert "Fetcher: plain" in shown
    assert "Verdict: accepted" in shown
    assert "Confidence: accepted" in shown
    assert "Title: How Raspberry Pi 5 Handles Sustained Workloads" in shown
    assert "## What we saw" in shown


def test_thin_page_comes_back_flagged_possibly_partial(mcp: McpCaller, web: FakeWeb) -> None:
    url = web.serve("/changelog/4.2", page("short_note.html"))

    result = mcp.call("fetch", url=url)

    assert result.isError is False
    assert data(result)["verdict"] == "suspect"
    assert data(result)["confidence"] == "possibly_partial"
    assert "Confidence: possibly_partial" in text(result)
    assert "arm64 container images" in text(result)


def test_block_page_is_an_explicit_failure_listing_every_fetcher_tried(
    mcp: McpCaller, web: FakeWeb
) -> None:
    url = web.serve("/consent", page("cookie_wall.html"))

    result = mcp.call("fetch", url=url)

    assert result.isError is True
    got = data(result)
    assert got["code"] == "SCRAPE_REJECTED"
    assert [(a["fetcher"], a["verdict"], a["reason"]) for a in got["attempts"]][0] == (
        "plain",
        "rejected",
        "cookie_wall",
    )
    assert [a["fetcher"] for a in got["attempts"]] == ["plain", "paid"]
    shown = text(result)
    assert f"No Fetcher produced real content for {url}" in shown
    assert "plain: rejected (cookie_wall); paid: rejected" in shown
    assert "We value your privacy" not in shown


def test_exhausted_paid_quota_is_the_headline_of_the_failure(
    mcp: McpCaller, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    url = web.serve("/consent", page("cookie_wall.html"))
    firecrawl.quota_exhausted = True

    result = mcp.call("fetch", url=url)

    assert result.isError is True
    assert data(result)["code"] == "FETCHER_UNAVAILABLE"
    assert text(result).startswith("Paid Fetcher unavailable: quota exhausted")


def test_forced_fetcher_is_the_only_one_tried(
    mcp: McpCaller, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    url = web.serve("/article", page("article.html"))
    firecrawl.serve(url, firecrawl_page("article.html", ARTICLE_MARKDOWN))

    result = mcp.call("fetch", url=url, fetcher="paid")

    assert result.isError is False
    assert data(result)["fetcher"] == "paid"
    assert [a["fetcher"] for a in data(result)["attempts"]] == ["paid"]
    assert web.hits("/article") == 0


def test_unknown_forced_fetcher_is_an_error_naming_the_choices(
    mcp: McpCaller, web: FakeWeb
) -> None:
    url = web.serve("/article", page("article.html"))

    result = mcp.call("fetch", url=url, fetcher="chrome")

    assert result.isError is True
    assert "unknown Fetcher or Site Adapter 'chrome'; expected one of: plain, paid" in text(result)
    assert web.hits("/article") == 0


def test_repeat_fetch_is_served_from_the_cache_and_max_age_zero_forces_a_live_fetch(
    mcp: McpCaller, web: FakeWeb
) -> None:
    url = web.serve("/article", page("article.html"))

    mcp.call("fetch", url=url)
    cached = mcp.call("fetch", url=url)

    assert web.hits("/article") == 1
    assert data(cached)["cachedAt"] is not None
    assert data(cached)["attempts"] == []
    assert "Cached at:" in text(cached)

    live = mcp.call("fetch", url=url, max_age_seconds=0)

    assert web.hits("/article") == 2
    assert data(live)["cachedAt"] is None


def test_negative_max_age_is_refused(mcp: McpCaller, web: FakeWeb) -> None:
    url = web.serve("/article", page("article.html"))

    result = mcp.call("fetch", url=url, max_age_seconds=-1)

    assert result.isError is True
    assert web.hits("/article") == 0


def test_reachable_by_the_pis_tailnet_name(config: Config, web: FakeWeb) -> None:
    # Claude Code on the Mac and GM-Desktop connects to http://pi5:3002/mcp, so the
    # server must accept a Host that isn't localhost.
    caller = McpCaller(create_app(config), host_header="pi5:3002")
    caller.start()
    try:
        url = web.serve("/article", page("article.html"))
        result = caller.call("fetch", url=url)
    finally:
        caller.stop()

    assert result.isError is False
    assert data(result)["fetcher"] == "plain"


def test_one_plain_json_rpc_call_lists_the_tools(mcp: McpCaller) -> None:
    # The MCP endpoint keeps no sessions, so a single POST works without initialize:
    # what deploy/pi5/verify-from-mac.sh relies on, and why a redeploy strands no one.
    response = httpx.post(
        mcp.url,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"accept": "application/json, text/event-stream"},
        timeout=30,
    )

    assert response.status_code == 200
    assert "fetch" in [t["name"] for t in response.json()["result"]["tools"]]


def test_scrape_api_still_answers_next_to_mcp(mcp: McpCaller, web: FakeWeb) -> None:
    url = web.serve("/article", page("article.html"))
    base = mcp.url.removesuffix("/mcp")

    response = httpx.post(f"{base}/v1/scrape", json={"url": url}, timeout=30)

    assert response.status_code == 200
    assert response.json()["data"]["fetcher"] == "plain"
