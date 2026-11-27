"""Seam 1: the MCP `search` tool, called over Streamable HTTP the way Claude Code calls
it, against fake Search Providers."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from scraper.api import create_app
from scraper.config import Config

from .fakefirecrawl import FakeFirecrawl
from .fakesearch import FakeSearchProviders, Hit
from .mcpcaller import McpCaller
from .test_mcp_fetch import data, text


@pytest.fixture
def mcp(config: Config) -> Iterator[McpCaller]:
    caller = McpCaller(create_app(config))
    caller.start()
    yield caller
    caller.stop()


def test_search_tool_is_listed_with_query_and_optional_limit(mcp: McpCaller) -> None:
    (tool,) = [t for t in mcp.tools() if t.name == "search"]

    assert set(tool.inputSchema["properties"]) == {"query", "limit"}
    assert tool.inputSchema["required"] == ["query"]
    assert tool.annotations is not None and tool.annotations.readOnlyHint is True


def test_search_returns_title_url_snippet_and_provider(
    mcp: McpCaller, providers: FakeSearchProviders
) -> None:
    providers.fail("tinyfish")
    providers.answer(
        "exa",
        [
            Hit("Pi 5 review", "https://example.org/review", "A long review.", "2026-10-14"),
            Hit("Pi 5 thermals", "https://example.org/thermals", "Heat under load."),
        ],
    )

    result = mcp.call("search", query="pi 5 review")

    assert result.isError is False
    got = data(result)
    assert got["results"] == [
        {
            "title": "Pi 5 review",
            "url": "https://example.org/review",
            "snippet": "A long review.",
            "provider": "exa",
            "publishedDate": "2026-10-14",
        },
        {
            "title": "Pi 5 thermals",
            "url": "https://example.org/thermals",
            "snippet": "Heat under load.",
            "provider": "exa",
            "publishedDate": "unknown",
        },
    ]
    assert got["provider"] == "exa"
    assert got["cachedAt"] is None
    assert [a["provider"] for a in got["attempts"]] == ["tinyfish"]
    # The model reads the same results.
    shown = text(result)
    assert "Provider: exa" in shown
    assert "1. Pi 5 review" in shown
    assert "https://example.org/thermals" in shown
    assert "Heat under load." in shown


def test_search_limit_caps_the_results(mcp: McpCaller, providers: FakeSearchProviders) -> None:
    result = mcp.call("search", query="pi 5 review", limit=2)

    assert len(data(result)["results"]) == 2


def test_repeat_search_is_served_from_the_cache(
    mcp: McpCaller, providers: FakeSearchProviders
) -> None:
    mcp.call("search", query="pi 5 review")
    result = mcp.call("search", query="pi 5 review")

    assert data(result)["cachedAt"] is not None
    assert "Cached at:" in text(result)
    assert providers.queries["tinyfish"] == ["pi 5 review"]


def test_every_provider_failing_is_a_tool_error_naming_each_provider(
    mcp: McpCaller, providers: FakeSearchProviders, firecrawl: FakeFirecrawl
) -> None:
    for name in ("tinyfish", "exa", "tavily"):
        providers.run_out(name)
    providers.fail("duckduckgo")

    result = mcp.call("search", query="pi 5 review")

    assert result.isError is True
    got = data(result)
    assert got["code"] == "SEARCH_FAILED"
    assert [a["provider"] for a in got["attempts"]] == [
        "tinyfish",
        "exa",
        "tavily",
        "duckduckgo",
    ]
    shown = text(result)
    assert "Every Search Provider failed for 'pi 5 review'" in shown
    assert "duckduckgo: answered HTTP 500" in shown
    assert firecrawl.paths == []
