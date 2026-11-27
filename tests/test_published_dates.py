"""Seam 1: every search result and fetched page carries a Published date (YYYY-MM-DD)
or "unknown", the same way through /v1 and /v2 scrape and search and the MCP tools."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from scraper.api import create_app
from scraper.config import Config

from .fakefirecrawl import FakeFirecrawl, FirecrawlPage
from .fakesearch import FakeSearchProviders, Hit
from .fakeweb import FakeWeb, page
from .mcpcaller import McpCaller
from .test_mcp_fetch import data, text
from .test_search import search_v2

# 2027-01-15T08:00:00Z
NOW = 1_800_000_000.0


@pytest.fixture
def clocked(config: Config) -> TestClient:
    return TestClient(create_app(config, clock=lambda: NOW))


def dated(date: str | None, n: int = 1) -> Hit:
    return Hit(f"Result {n}", f"https://example.org/{n}", "Some text.", date)


@pytest.mark.parametrize(
    ("provider", "reported", "shown"),
    [
        ("exa", "2026-10-14T09:30:00.000Z", "2026-10-14"),
        ("exa", "2026-10-14", "2026-10-14"),
        ("tavily", "Wed, 14 Oct 2026 09:30:00 GMT", "2026-10-14"),
        ("tinyfish", "Oct 14, 2026", "2026-10-14"),
        ("tinyfish", "14 October 2026", "2026-10-14"),
        ("tinyfish", "Sept 14, 2026", "2026-09-14"),
        ("tinyfish", "Oct. 14, 2026", "2026-10-14"),
        ("tavily", "2026-10-14 10:00:00 UTC", "2026-10-14"),
        ("tinyfish", "2 hrs ago", "2027-01-15"),
        ("tinyfish", "2099-01-01", "unknown"),
        ("tinyfish", "3 days ago", "2027-01-12"),
        ("tinyfish", "5 hours ago", "2027-01-15"),
        ("tinyfish", "sometime last spring", "unknown"),
        ("tinyfish", None, "unknown"),
    ],
)
def test_a_providers_date_is_normalised_or_unknown(
    clocked: TestClient,
    providers: FakeSearchProviders,
    provider: str,
    reported: str | None,
    shown: str,
) -> None:
    for name in ("tinyfish", "exa", "tavily"):
        if name != provider:
            providers.fail(name)
    providers.answer(provider, [dated(reported)])

    _, body = search_v2(clocked, "pi 5 review")

    assert body["provider"] == provider
    assert body["data"]["web"][0]["publishedDate"] == shown


def test_duckduckgo_results_have_unknown_dates(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    for name in ("tinyfish", "exa", "tavily"):
        providers.fail(name)

    _, body = search_v2(client, "pi 5 review")

    assert {r["publishedDate"] for r in body["data"]["web"]} == {"unknown"}


def test_a_relative_date_in_a_cached_search_counts_from_when_it_was_searched(
    config: Config, providers: FakeSearchProviders
) -> None:
    now = [NOW]
    client = TestClient(create_app(config, clock=lambda: now[0]))
    providers.answer("tinyfish", [dated("2 days ago")])
    search_v2(client, "pi 5 review")
    now[0] += 20 * 3600

    _, body = search_v2(client, "pi 5 review")

    assert body["cacheState"] == "hit"
    assert body["data"]["web"][0]["publishedDate"] == "2027-01-13"


def test_a_result_without_a_date_takes_it_from_the_fetched_page(
    client: TestClient, providers: FakeSearchProviders, web: FakeWeb
) -> None:
    url = web.serve("/article", page("article.html"))
    client.post("/v1/scrape", json={"url": url})
    providers.answer("tinyfish", [Hit("Pi 5 article", url, "Thermals."), dated(None, 2)])

    _, body = search_v2(client, "pi 5 review")

    assert [r["publishedDate"] for r in body["data"]["web"]] == ["2026-10-14", "unknown"]


def test_a_page_cached_with_a_raw_date_is_shown_normalised(
    config: Config, web: FakeWeb, providers: FakeSearchProviders
) -> None:
    from dataclasses import replace

    from scraper.cache import PageCache
    from scraper.gate import Verdict
    from scraper.page import build_page

    # A copy cached before dates were normalised, as Firecrawl wrote it.
    url = web.serve("/changelog/4.2", page("short_note.html"))
    built = build_page(url, url, 200, {}, page("short_note.html").body.decode())
    PageCache(config.cache, lambda: NOW).put(
        url, replace(built, published="2026-09-30T22:15:00+02:00"), "paid",
        Verdict("suspect", "thin", ""),
    )
    client = TestClient(create_app(config, clock=lambda: NOW))
    providers.answer("tinyfish", [Hit("Changelog", url, "Notes.")])

    scraped = client.post("/v2/scrape", json={"url": url}).json()
    _, searched = search_v2(client, "changelog")

    assert scraped["data"]["metadata"]["cacheState"] == "hit"
    assert scraped["data"]["publishedDate"] == "2026-09-30"
    assert searched["data"]["web"][0]["publishedDate"] == "2026-09-30"


def test_v1_search_results_carry_the_date(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    providers.answer("tinyfish", [dated("2026-10-14T09:30:00Z")])

    body = client.post("/v1/search", json={"query": "pi 5 review"}).json()

    assert body["data"][0]["publishedDate"] == "2026-10-14"


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_a_page_without_a_date_is_published_unknown(
    client: TestClient, web: FakeWeb, version: str
) -> None:
    url = web.serve("/changelog/4.2", page("short_note.html"))

    body = client.post(f"/{version}/scrape", json={"url": url}).json()

    assert body["success"] is True
    assert body["data"]["publishedDate"] == "unknown"


def test_the_paid_fetchers_date_is_normalised(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    url = web.serve("/blocked", page("cf_challenge.html", status=403))
    firecrawl.serve(
        url,
        FirecrawlPage(
            markdown="Real content. " * 100,
            raw_html="",
            title="A real page",
            extra_metadata={"publishedTime": "2026-09-30T22:15:00+02:00"},
        ),
    )

    body = client.post("/v2/scrape", json={"url": url}).json()

    assert body["data"]["fetcher"] == "paid"
    assert body["data"]["publishedDate"] == "2026-09-30"


# ── MCP tools ─────────────────────────────────────────────────────────────────


@pytest.fixture
def mcp(config: Config) -> Iterator[McpCaller]:
    caller = McpCaller(create_app(config))
    caller.start()
    yield caller
    caller.stop()


def test_mcp_search_shows_each_results_date(
    mcp: McpCaller, providers: FakeSearchProviders
) -> None:
    providers.answer("tinyfish", [dated("2026-10-14", 1), dated(None, 2)])

    result = mcp.call("search", query="pi 5 review")

    assert [r["publishedDate"] for r in data(result)["results"]] == ["2026-10-14", "unknown"]
    shown = text(result)
    assert "Published: 2026-10-14" in shown
    assert "Published: unknown" in shown


def test_mcp_fetch_of_a_page_without_a_date_says_unknown(
    mcp: McpCaller, web: FakeWeb
) -> None:
    url = web.serve("/changelog/4.2", page("short_note.html"))

    result = mcp.call("fetch", url=url)

    assert data(result)["publishedDate"] == "unknown"
    assert "Published: unknown" in text(result)
