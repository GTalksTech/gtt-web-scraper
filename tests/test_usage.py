"""Seam 1: a month's usage, as the monthly summary reads it from /health/usage. Callers'
page and search requests counted by what served them, the Canary Set's checks counted
apart, and each Fetcher's attempts (the Paid Fetcher's are Firecrawl credits spent)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from scraper.api import create_app
from scraper.config import Config
from scraper.health.canary import CHECK_HEADER

from .fakefirecrawl import FakeFirecrawl, firecrawl_page
from .fakesearch import FakeSearchProviders
from .fakeweb import FakeWeb, page
from .test_cache import FakeClock
from .test_paid_fetcher import ARTICLE_MARKDOWN
from .test_scrape import CF_HEADERS, scrape
from .test_search import search_v2

DAY_S = 24 * 60 * 60
MONTH = "2026-09"  # FakeClock starts on 2026-09-21


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(config: Config, clock: FakeClock) -> TestClient:
    return TestClient(create_app(config, clock=clock))


def usage(client: TestClient, month: str = MONTH) -> dict[str, Any]:
    response = client.get("/health/usage", params={"month": month})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_a_new_service_has_served_nothing(client: TestClient) -> None:
    seen = usage(client)

    assert seen["month"] == MONTH
    assert seen["pages"] == {"requests": 0, "servedBy": {}}
    assert seen["searches"] == {"requests": 0, "servedBy": {}}
    assert seen["checks"] == {"pages": 0, "searches": 0}
    assert [(f["fetcher"], f["attempts"]) for f in seen["fetchers"]] == [
        ("plain", 0),
        ("chrome", 0),
        ("paid", 0),
    ]


def test_page_requests_are_counted_by_what_served_them(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    article = web.serve("/article", page("article.html"))
    blocked = web.serve("/blocked", page("cf_turnstile.html", 403, CF_HEADERS), host="localhost")
    firecrawl.serve(blocked, firecrawl_page("article.html", ARTICLE_MARKDOWN))
    missing = web.serve("/gone", page("article.html", 404))
    scrape(client, article)  # plain download
    scrape(client, article)  # the cache
    scrape(client, blocked)  # plain rejected, then the Paid Fetcher
    scrape(client, missing)  # nothing worked

    pages = usage(client)["pages"]

    assert pages == {"requests": 4, "servedBy": {"plain": 1, "cache": 1, "paid": 1, "failed": 1}}


def test_paid_fetcher_use_is_its_attempts_and_the_requests_its_quota_refused(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    blocked = web.serve("/blocked", page("cf_turnstile.html", 403, CF_HEADERS), host="localhost")
    firecrawl.serve(blocked, firecrawl_page("article.html", ARTICLE_MARKDOWN))
    scrape(client, blocked)
    firecrawl.quota_exhausted = True
    scrape(client, blocked, maxAge=0)

    [paid] = [f for f in usage(client)["fetchers"] if f["fetcher"] == "paid"]

    assert (paid["attempts"], paid["successes"], paid["unavailable"]) == (1, 1, 1)


def test_search_requests_are_counted_by_what_served_them(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    providers.fail("tinyfish")
    search_v2(client, "raspberry pi 5 thermals")  # Exa, after TinyFish failed
    search_v2(client, "raspberry pi 5 thermals")  # the cache
    for name in ("exa", "tavily", "duckduckgo", "searxng"):
        providers.fail(name)
    search_v2(client, "something else")  # every Provider failed

    searches = usage(client)["searches"]

    assert searches == {"requests": 3, "servedBy": {"exa": 1, "cache": 1, "failed": 1}}


def test_the_canary_sets_checks_are_counted_apart_from_callers(
    client: TestClient, web: FakeWeb
) -> None:
    article = web.serve("/article", page("article.html"))
    client.post("/v1/scrape", json={"url": article, "maxAge": 0}, headers=CHECK_HEADER)
    client.post("/v2/search", json={"query": "pi"}, headers=CHECK_HEADER)
    scrape(client, article)

    seen = usage(client)

    assert seen["pages"] == {"requests": 1, "servedBy": {"cache": 1}}
    assert seen["searches"]["requests"] == 0
    assert seen["checks"] == {"pages": 1, "searches": 1}


def test_only_the_months_requests_count(
    client: TestClient, clock: FakeClock, web: FakeWeb
) -> None:
    article = web.serve("/article", page("article.html"))
    scrape(client, article)  # September
    clock.advance(15 * DAY_S)
    scrape(client, article, maxAge=0)  # October
    scrape(client, article, maxAge=0)

    assert usage(client, "2026-09")["pages"]["requests"] == 1
    assert usage(client, "2026-10")["pages"]["requests"] == 2
    [plain] = [f for f in usage(client, "2026-10")["fetchers"] if f["fetcher"] == "plain"]
    assert plain["attempts"] == 2


def test_last_months_usage_is_still_there_at_the_start_of_the_next_month_but_one(
    client: TestClient, clock: FakeClock, web: FakeWeb
) -> None:
    # The summary for a 31-day month runs on the 1st of the next: up to 32 days later.
    scrape(client, web.serve("/article", page("article.html")))
    clock.advance(45 * DAY_S)
    scrape(client, web.serve("/other", page("article.html")))  # prunes old rows

    assert usage(client, "2026-09")["pages"]["requests"] == 1


def test_a_malformed_month_is_refused(client: TestClient) -> None:
    assert client.get("/health/usage", params={"month": "September"}).status_code == 422
