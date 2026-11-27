"""Seam 1: per-site rate limits, cool-downs, per-site memory and forced Fetchers,
driven through the scrape endpoint.

The fake web answers on both 127.0.0.1 and localhost, which the service treats as
two different sites. A fake clock stands in for cool-downs and memory expiring.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from scraper.api import create_app
from scraper.config import Config

from .fakefirecrawl import FakeFirecrawl, firecrawl_page
from .fakeweb import FakeResponse, FakeWeb, page
from .test_cache import FakeClock
from .test_paid_fetcher import ARTICLE_MARKDOWN, tried
from .test_scrape import CF_HEADERS, scrape

COOLDOWN_S = 30 * 60
MEMORY_TTL_S = 7 * 24 * 60 * 60


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(config: Config, clock: FakeClock) -> TestClient:
    config.sites.cooldown_s = COOLDOWN_S
    config.sites.memory_ttl_s = MEMORY_TTL_S
    return TestClient(create_app(config, clock=clock))


# --- Spacing -------------------------------------------------------------------------


def test_requests_to_one_site_are_spaced_by_the_minimum_interval(
    config: Config, web: FakeWeb
) -> None:
    config.sites.min_interval_s = 0.3
    client = TestClient(create_app(config))
    first = web.serve("/one", page("article.html"))
    second = web.serve("/two", page("article.html"))

    scrape(client, first)
    scrape(client, second)

    one, two = web.arrivals("/one", "/two")
    assert two - one >= 0.3


def test_challenge_polls_are_spaced_too(config: Config, web: FakeWeb) -> None:
    config.sites.min_interval_s = 0.2
    client = TestClient(create_app(config))
    url = web.serve("/news", page("cf_challenge.html", 403, CF_HEADERS), page("article.html"))

    status, _ = scrape(client, url)

    assert status == 200
    challenged, cleared = web.arrivals("/news")
    assert cleared - challenged >= 0.2


def test_requests_to_different_sites_are_not_spaced(config: Config, web: FakeWeb) -> None:
    config.sites.min_interval_s = 30.0
    client = TestClient(create_app(config))
    first = web.serve("/one", page("article.html"))
    second = web.serve("/two", page("article.html"), host="localhost")

    started = time.monotonic()
    scrape(client, first)
    scrape(client, second)

    assert time.monotonic() - started < 30.0
    assert web.hits("/one") == web.hits("/two") == 1


def test_paid_fetches_are_not_spaced(
    config: Config, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    # The Paid Fetcher reaches the site from Firecrawl, not over the home connection.
    config.sites.min_interval_s = 30.0
    client = TestClient(create_app(config))
    first = web.serve("/one", page("article.html"))
    second = web.serve("/two", page("soft_404.html"))
    firecrawl.serve(second, firecrawl_page("article.html", ARTICLE_MARKDOWN))

    started = time.monotonic()
    scrape(client, first)
    status, body = scrape(client, second, fetcher="paid")

    assert time.monotonic() - started < 30.0
    assert status == 200
    assert body["data"]["fetcher"] == "paid"


# --- Cool-downs ----------------------------------------------------------------------

BLOCKS = {
    "403 challenge": page("cf_turnstile.html", 403, CF_HEADERS),
    "429": page("article.html", 429),
    "403": page("article.html", 403),
    "challenge on a 200": page("cf_challenge.html"),
}


@pytest.mark.parametrize("block", BLOCKS.values(), ids=BLOCKS.keys())
def test_a_block_puts_the_site_on_a_cool_down_that_skips_the_plain_download(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl, block: FakeResponse
) -> None:
    blocked = web.serve("/news", block)
    later = web.serve("/sport", page("article.html"))
    # Only the later page is on Firecrawl, so the site isn't remembered as needing the
    # Paid Fetcher: only the cool-down keeps the plain download away.
    firecrawl.serve(later, firecrawl_page("article.html", ARTICLE_MARKDOWN))

    scrape(client, blocked)
    status, body = scrape(client, later)

    assert status == 200
    assert web.hits("/sport") == 0
    assert body["data"]["fetcher"] == "paid"
    assert tried(body["data"]) == [
        ("plain", "rejected", "cooling_down"),
        ("paid", "accepted", "ok"),
    ]
    assert "127.0.0.1 is on a cool-down" in body["data"]["attempts"][0]["detail"]


def test_a_rejection_that_is_not_a_block_does_not_start_a_cool_down(
    client: TestClient, web: FakeWeb
) -> None:
    missing = web.serve("/old", page("soft_404.html"))
    article = web.serve("/article", page("article.html"))

    scrape(client, missing)
    status, body = scrape(client, article)

    assert status == 200
    assert body["data"]["fetcher"] == "plain"


def test_a_cool_down_covers_only_the_site_that_blocked(
    client: TestClient, web: FakeWeb
) -> None:
    blocked = web.serve("/news", page("article.html", 429))
    elsewhere = web.serve("/article", page("article.html"), host="localhost")

    scrape(client, blocked)
    status, body = scrape(client, elsewhere)

    assert status == 200
    assert body["data"]["fetcher"] == "plain"


def test_the_plain_download_is_tried_again_once_the_cool_down_expires(
    client: TestClient, clock: FakeClock, web: FakeWeb
) -> None:
    blocked = web.serve("/news", page("article.html", 429))
    article = web.serve("/article", page("article.html"))

    scrape(client, blocked)
    clock.advance(COOLDOWN_S - 60)
    _, during = scrape(client, article)
    clock.advance(120)
    status, after = scrape(client, article)

    assert tried(during)[0] == ("plain", "rejected", "cooling_down")
    assert status == 200
    assert after["data"]["fetcher"] == "plain"
    assert web.hits("/article") == 1


def test_failure_during_a_cool_down_says_the_site_is_cooling_down(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    blocked = web.serve("/news", page("article.html", 429))
    later = web.serve("/sport", page("article.html"))
    firecrawl.quota_exhausted = True

    scrape(client, blocked)
    status, body = scrape(client, later)

    assert status == 502
    assert body["code"] == "FETCHER_UNAVAILABLE"
    assert body["error"].startswith("plain skipped: 127.0.0.1 is on a cool-down until")
    assert "(http_429)" in body["error"]
    assert "Paid Fetcher unavailable: quota exhausted" in body["error"]
    assert web.hits("/sport") == 0


def test_cool_downs_survive_a_restart(
    config: Config, client: TestClient, clock: FakeClock, web: FakeWeb
) -> None:
    blocked = web.serve("/news", page("article.html", 429))
    article = web.serve("/article", page("article.html"))
    scrape(client, blocked)

    restarted = TestClient(create_app(config, clock=clock))
    _, body = scrape(restarted, article)

    assert tried(body)[0] == ("plain", "rejected", "cooling_down")
    assert web.hits("/article") == 0


# --- Per-site memory -----------------------------------------------------------------


def needs_paid(web: FakeWeb, firecrawl: FakeFirecrawl, path: str, host: str = "127.0.0.1") -> str:
    """A page the plain download can't get (a soft 404, which is no block) but the Paid
    Fetcher can."""
    url = web.serve(path, page("soft_404.html"), host=host)
    firecrawl.serve(url, firecrawl_page("article.html", ARTICLE_MARKDOWN))
    return url


def test_next_request_to_a_site_starts_at_the_fetcher_that_last_succeeded(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    first = needs_paid(web, firecrawl, "/one")
    second = needs_paid(web, firecrawl, "/two")

    scrape(client, first)
    status, body = scrape(client, second)

    assert status == 200
    assert tried(body["data"]) == [("paid", "accepted", "ok")]
    assert web.hits("/two") == 0


def test_when_the_remembered_fetcher_fails_the_rest_of_the_ladder_is_tried(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    scrape(client, needs_paid(web, firecrawl, "/one"))
    # Firecrawl has nothing for this one; the plain download does.
    plain_page = web.serve("/two", page("article.html"))
    after = web.serve("/three", page("article.html"))

    _, second = scrape(client, plain_page)
    _, third = scrape(client, after)

    assert tried(second["data"]) == [
        ("paid", "rejected", "fetch_error"),
        ("plain", "accepted", "ok"),
    ]
    # The plain download worked last, so it goes first again.
    assert tried(third["data"]) == [("plain", "accepted", "ok")]


def test_per_site_memory_is_per_site(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    scrape(client, needs_paid(web, firecrawl, "/one"))
    elsewhere = web.serve("/article", page("article.html"), host="localhost")

    _, body = scrape(client, elsewhere)

    assert tried(body["data"]) == [("plain", "accepted", "ok")]


def test_remembered_fetcher_is_trusted_only_for_a_while_after_it_first_worked(
    client: TestClient, clock: FakeClock, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    scrape(client, needs_paid(web, firecrawl, "/one"))
    clock.advance(MEMORY_TTL_S - 60)
    # Another success with the same Fetcher doesn't extend the trust...
    _, within = scrape(client, needs_paid(web, firecrawl, "/two"))
    clock.advance(120)
    # ...so a week after it first worked, a request climbs from the bottom again.
    _, after = scrape(client, needs_paid(web, firecrawl, "/three"))
    _, next_one = scrape(client, needs_paid(web, firecrawl, "/four"))

    assert tried(within["data"]) == [("paid", "accepted", "ok")]
    assert tried(after["data"]) == [("plain", "rejected", "soft_404"), ("paid", "accepted", "ok")]
    # Having needed the Paid Fetcher again, the site is trusted to it afresh.
    assert tried(next_one["data"]) == [("paid", "accepted", "ok")]


def test_per_site_memory_survives_a_restart(
    config: Config, client: TestClient, clock: FakeClock, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    scrape(client, needs_paid(web, firecrawl, "/one"))

    restarted = TestClient(create_app(config, clock=clock))
    _, body = scrape(restarted, needs_paid(web, firecrawl, "/two"))

    assert tried(body["data"]) == [("paid", "accepted", "ok")]


# --- Forced Fetchers -----------------------------------------------------------------


def test_caller_can_force_the_paid_fetcher(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    url = web.serve("/hard", page("article.html"))
    firecrawl.serve(url, firecrawl_page("article.html", ARTICLE_MARKDOWN))

    status, body = scrape(client, url, fetcher="paid")

    assert status == 200
    assert tried(body["data"]) == [("paid", "accepted", "ok")]
    assert web.hits("/hard") == 0


def test_a_forced_fetcher_that_fails_does_not_escalate(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    url = web.serve("/old", page("soft_404.html"))
    firecrawl.serve(url, firecrawl_page("article.html", ARTICLE_MARKDOWN))

    status, body = scrape(client, url, fetcher="plain")

    assert status == 502
    assert tried(body) == [("plain", "rejected", "soft_404")]
    assert firecrawl.scrapes == []


def test_a_forced_fetcher_overrides_per_site_memory(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    scrape(client, needs_paid(web, firecrawl, "/one"))
    url = web.serve("/article", page("article.html"))

    _, body = scrape(client, url, fetcher="plain")

    assert tried(body["data"]) == [("plain", "accepted", "ok")]
    assert len(firecrawl.scrapes) == 1


def test_forcing_the_plain_download_does_not_override_a_cool_down(
    client: TestClient, web: FakeWeb
) -> None:
    blocked = web.serve("/news", page("article.html", 429))
    article = web.serve("/article", page("article.html"))
    scrape(client, blocked)

    status, body = scrape(client, article, fetcher="plain")

    assert status == 502
    assert tried(body) == [("plain", "rejected", "cooling_down")]
    assert web.hits("/article") == 0


def test_forcing_an_unknown_fetcher_is_refused(client: TestClient, web: FakeWeb) -> None:
    url = web.serve("/article", page("article.html"))

    status, body = scrape(client, url, fetcher="chrome")

    assert status == 422
    assert body["success"] is False
    assert body["code"] == "UNKNOWN_FETCHER"
    assert "plain, paid" in body["error"]
    assert web.hits("/article") == 0


def test_v2_scrape_accepts_a_forced_fetcher_too(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    url = web.serve("/hard", page("article.html"))
    firecrawl.serve(url, firecrawl_page("article.html", ARTICLE_MARKDOWN))

    body = client.post("/v2/scrape", json={"url": url, "fetcher": "paid"}).json()

    assert body["data"]["fetcher"] == "paid"
    assert web.hits("/hard") == 0
