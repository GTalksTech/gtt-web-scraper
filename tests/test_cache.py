"""Seam 1: the page cache and Firecrawl's maxAge, driven through the scrape endpoint.

A fake clock stands in for time passing, so expiry is tested without waiting.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from scraper.api import create_app
from scraper.config import Config

from .fakeweb import FakeWeb, page
from .test_scrape import scrape

DAY_S = 24 * 60 * 60


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_790_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_client(config: Config) -> tuple[TestClient, FakeClock]:
    clock = FakeClock()
    return TestClient(create_app(config, clock=clock)), clock


def test_page_fetched_within_a_day_is_served_from_the_cache_without_touching_the_site(
    config: Config, web: FakeWeb
) -> None:
    client, clock = make_client(config)
    url = web.serve("/article", page("article.html"))

    _, first = scrape(client, url)
    clock.advance(DAY_S - 60)
    status, second = scrape(client, url)

    assert web.hits("/article") == 1
    assert status == 200
    assert second["success"] is True
    assert second["data"]["markdown"] == first["data"]["markdown"]
    assert second["data"]["metadata"]["title"] == first["data"]["metadata"]["title"]
    assert second["data"]["fetcher"] == "plain"
    assert second["data"]["verdict"] == "accepted"
    assert second["data"]["confidence"] == "accepted"
    assert second["data"]["publishedDate"] == "2026-10-14"


def test_responses_say_whether_they_came_from_the_cache(config: Config, web: FakeWeb) -> None:
    client, clock = make_client(config)
    url = web.serve("/article", page("article.html"))

    _, live = scrape(client, url)
    clock.advance(90)
    _, cached = scrape(client, url)

    assert live["data"]["metadata"]["cacheState"] == "miss"
    assert "cachedAt" not in live["data"]["metadata"]
    assert cached["data"]["metadata"]["cacheState"] == "hit"
    # When the page was fetched (the fake clock's start), not when it was served.
    assert cached["data"]["metadata"]["cachedAt"] == "2026-09-21T14:13:20Z"
    # Nothing was tried for the cached response.
    assert cached["data"]["attempts"] == []


def test_cached_page_older_than_a_day_is_fetched_live_again(
    config: Config, web: FakeWeb
) -> None:
    client, clock = make_client(config)
    url = web.serve("/article", page("article.html"))

    scrape(client, url)
    clock.advance(DAY_S + 60)
    status, body = scrape(client, url)

    assert status == 200
    assert web.hits("/article") == 2
    assert body["data"]["metadata"]["cacheState"] == "miss"


def test_max_age_in_milliseconds_overrides_the_default(config: Config, web: FakeWeb) -> None:
    client, clock = make_client(config)
    url = web.serve("/article", page("article.html"))

    scrape(client, url)
    clock.advance(10 * 60)
    _, too_old = scrape(client, url, maxAge=5 * 60 * 1000)
    clock.advance(2 * DAY_S)
    _, old_enough = scrape(client, url, maxAge=3 * DAY_S * 1000)

    assert too_old["data"]["metadata"]["cacheState"] == "miss"
    assert old_enough["data"]["metadata"]["cacheState"] == "hit"
    assert web.hits("/article") == 2


def test_max_age_zero_forces_a_live_fetch_and_refreshes_the_cache(
    config: Config, web: FakeWeb
) -> None:
    client, clock = make_client(config)
    url = web.serve("/changelog", page("article.html"), page("short_note.html"))

    scrape(client, url)
    clock.advance(60)
    _, forced = scrape(client, url, maxAge=0)
    clock.advance(60)
    _, later = scrape(client, url)

    assert web.hits("/changelog") == 2
    assert forced["data"]["metadata"]["cacheState"] == "miss"
    assert "arm64 container images" in forced["data"]["markdown"]
    # The forced fetch replaced the cached copy.
    assert later["data"]["metadata"]["cacheState"] == "hit"
    assert "arm64 container images" in later["data"]["markdown"]


def test_negative_max_age_is_refused(config: Config, web: FakeWeb) -> None:
    client, _ = make_client(config)
    url = web.serve("/article", page("article.html"))

    response = client.post("/v1/scrape", json={"url": url, "maxAge": -1})

    assert response.status_code == 422
    assert web.hits("/article") == 0


def test_suspect_results_are_cached(config: Config, web: FakeWeb) -> None:
    client, _ = make_client(config)
    url = web.serve("/changelog/4.2", page("short_note.html"))

    scrape(client, url)
    status, body = scrape(client, url)

    assert status == 200
    assert web.hits("/changelog/4.2") == 1
    assert body["data"]["metadata"]["cacheState"] == "hit"
    assert body["data"]["verdict"] == "suspect"
    assert body["data"]["confidence"] == "possibly_partial"


def test_failures_are_not_cached(config: Config, web: FakeWeb) -> None:
    client, _ = make_client(config)
    url = web.serve("/story", page("cookie_wall.html"), page("article.html"))

    first_status, _ = scrape(client, url)
    status, body = scrape(client, url)

    assert first_status == 502
    assert status == 200
    assert web.hits("/story") == 2
    assert body["data"]["metadata"]["cacheState"] == "miss"
    assert "Raspberry Pi 5 runs the" in body["data"]["markdown"]


def test_failed_forced_fetch_keeps_the_last_good_copy(config: Config, web: FakeWeb) -> None:
    client, clock = make_client(config)
    url = web.serve("/story", page("article.html"), page("cookie_wall.html"))

    scrape(client, url)
    clock.advance(60)
    forced_status, _ = scrape(client, url, maxAge=0)
    status, body = scrape(client, url)

    assert forced_status == 502
    assert status == 200
    assert body["data"]["metadata"]["cacheState"] == "hit"
    assert "Raspberry Pi 5 runs the" in body["data"]["markdown"]
