"""Seam 1: the Chrome Fetcher on the Ladder, between the plain download and the Paid
Fetcher, driven through the scrape endpoint.

Real Chrome is never started: the Engine is tests/fakechrome.py, which loads pages
from the local fake web and "runs" their JavaScript. A page that only renders in a
browser (js_app.html) is an empty shell to the plain download and real content to the
Chrome Fetcher.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from scraper.api import create_app
from scraper.config import Config, load_config

from .fakechrome import FakeChrome
from .fakefirecrawl import FakeFirecrawl, firecrawl_page
from .fakeweb import FakeWeb, page
from .test_paid_fetcher import ARTICLE_MARKDOWN, tried
from .test_scrape import CF_HEADERS, scrape


def test_the_ladder_is_plain_then_chrome_then_paid() -> None:
    assert load_config().ladder == ["plain", "chrome", "paid"]


def test_javascript_page_the_plain_download_rejects_comes_back_from_chrome(
    client: TestClient, web: FakeWeb, chrome: FakeChrome, firecrawl: FakeFirecrawl
) -> None:
    url = web.serve("/app/report", page("js_app.html"))

    status, body = scrape(client, url)

    assert status == 200
    data = body["data"]
    assert data["fetcher"] == "chrome"
    assert data["verdict"] == "accepted"
    assert data["confidence"] == "accepted"
    assert "Raspberry Pi 5 runs the" in data["markdown"]
    assert data["metadata"]["title"].startswith("How Raspberry Pi 5 Handles Sustained Workloads")
    assert data["metadata"]["statusCode"] == 200
    assert data["publishedDate"] == "2026-10-14"
    assert tried(data) == [("plain", "rejected", "empty_shell"), ("chrome", "accepted", "ok")]
    assert firecrawl.scrapes == []  # no quota spent


def test_page_the_plain_download_accepts_never_starts_chrome(
    client: TestClient, web: FakeWeb, chrome: FakeChrome
) -> None:
    url = web.serve("/article", page("article.html"))

    status, body = scrape(client, url)

    assert status == 200
    assert body["data"]["fetcher"] == "plain"
    assert chrome.launches == 0
    assert chrome.rendered == []


def test_page_chrome_also_rejects_escalates_to_the_paid_fetcher(
    client: TestClient, web: FakeWeb, chrome: FakeChrome, firecrawl: FakeFirecrawl
) -> None:
    url = web.serve("/dashboard", page("empty_shell.html"))
    firecrawl.serve(url, firecrawl_page("article.html", ARTICLE_MARKDOWN))

    status, body = scrape(client, url)

    assert status == 200
    assert body["data"]["fetcher"] == "paid"
    assert tried(body["data"]) == [
        ("plain", "rejected", "empty_shell"),
        ("chrome", "rejected", "empty_shell"),
        ("paid", "accepted", "ok"),
    ]
    assert chrome.rendered == [url]


def test_chromes_result_passes_the_content_gate(
    client: TestClient, web: FakeWeb, chrome: FakeChrome, firecrawl: FakeFirecrawl
) -> None:
    # Chrome gets the cookie wall too: it is consent text, never content.
    url = web.serve("/news/today", page("cookie_wall.html"))
    firecrawl.quota_exhausted = True

    status, body = scrape(client, url)

    assert status == 502
    assert body["success"] is False
    assert tried(body) == [
        ("plain", "rejected", "cookie_wall"),
        ("chrome", "rejected", "cookie_wall"),
        ("paid", "rejected", "quota_exhausted"),
    ]


def test_chrome_waits_out_a_challenge_that_clears(
    client: TestClient, web: FakeWeb, chrome: FakeChrome, firecrawl: FakeFirecrawl
) -> None:
    # The plain download gets an empty shell; Chrome is challenged once, then the
    # challenge clears and the app renders.
    url = web.serve(
        "/app/report",
        page("empty_shell.html"),
        page("cf_challenge.html", 403, CF_HEADERS),
        page("js_app.html"),
    )

    status, body = scrape(client, url)

    assert status == 200
    assert body["data"]["fetcher"] == "chrome"
    assert tried(body["data"]) == [("plain", "rejected", "empty_shell"), ("chrome", "accepted", "ok")]
    assert firecrawl.scrapes == []


def test_a_site_that_blocked_the_home_connection_skips_chrome_too(
    client: TestClient, web: FakeWeb, chrome: FakeChrome, firecrawl: FakeFirecrawl
) -> None:
    # Chrome uses the home connection: a cool-down started by the plain download's
    # block keeps it off the site, and the request goes straight to the Paid Fetcher.
    url = web.serve("/news", page("cf_turnstile.html", 403, CF_HEADERS))
    firecrawl.serve(url, firecrawl_page("article.html", ARTICLE_MARKDOWN))

    status, body = scrape(client, url)

    assert status == 200
    assert body["data"]["fetcher"] == "paid"
    assert tried(body["data"]) == [
        ("plain", "rejected", "interactive_challenge"),
        ("chrome", "rejected", "cooling_down"),
        ("paid", "accepted", "ok"),
    ]
    assert chrome.rendered == []


def test_a_site_chrome_worked_on_starts_at_chrome_next_time(
    client: TestClient, web: FakeWeb, chrome: FakeChrome
) -> None:
    first = web.serve("/app/one", page("js_app.html"))
    second = web.serve("/app/two", page("js_app.html"))
    scrape(client, first)

    status, body = scrape(client, second)

    assert status == 200
    assert tried(body["data"]) == [("chrome", "accepted", "ok")]
    assert web.hits("/app/two") == 1


def test_chrome_can_be_forced(client: TestClient, web: FakeWeb, chrome: FakeChrome) -> None:
    url = web.serve("/article", page("article.html"))

    status, body = scrape(client, url, fetcher="chrome")

    assert status == 200
    assert tried(body["data"]) == [("chrome", "accepted", "ok")]


def test_missing_chrome_is_unavailable_and_the_request_falls_to_the_paid_fetcher(
    client: TestClient, web: FakeWeb, chrome: FakeChrome, firecrawl: FakeFirecrawl
) -> None:
    chrome.missing = True
    url = web.serve("/app/report", page("js_app.html"))
    firecrawl.serve(url, firecrawl_page("article.html", ARTICLE_MARKDOWN))

    status, body = scrape(client, url)

    assert status == 200
    assert body["data"]["fetcher"] == "paid"
    assert tried(body["data"]) == [
        ("plain", "rejected", "empty_shell"),
        ("chrome", "rejected", "engine_unavailable"),
        ("paid", "accepted", "ok"),
    ]
    detail = body["data"]["attempts"][1]["detail"]
    assert detail.startswith("Chrome Fetcher unavailable:")
    assert "Chrome is not installed" in detail


def test_a_crashed_browser_fails_that_page_and_is_restarted_for_the_next(
    client: TestClient, web: FakeWeb, chrome: FakeChrome, firecrawl: FakeFirecrawl
) -> None:
    crashing = web.serve("/app/crash", page("js_app.html"))
    fine = web.serve("/app/fine", page("js_app.html"), host="localhost")
    chrome.crash_on.add(crashing)
    firecrawl.quota_exhausted = True

    status, body = scrape(client, crashing)
    assert status == 502
    assert tried(body)[1] == ("chrome", "rejected", "fetch_error")

    status, body = scrape(client, fine)
    assert status == 200
    assert body["data"]["fetcher"] == "chrome"
    assert chrome.launches == 2


def test_the_engine_restarts_after_the_configured_number_of_pages(
    config: Config, web: FakeWeb, chrome: FakeChrome
) -> None:
    config.chrome.restart_after_pages = 2
    client = TestClient(create_app(config))
    urls = [web.serve(f"/app/{n}", page("js_app.html")) for n in range(5)]

    for url in urls:
        status, body = scrape(client, url, fetcher="chrome")
        assert status == 200

    assert chrome.rendered == urls
    assert chrome.launches == 3  # pages 1-2, 3-4, 5
    assert chrome.closes == 2


def test_concurrent_pages_are_capped(config: Config, web: FakeWeb, chrome: FakeChrome) -> None:
    config.chrome.max_pages = 2
    client = TestClient(create_app(config))
    urls = [web.serve(f"/app/{n}", page("js_app.html")) for n in range(5)]
    chrome.hold = threading.Event()

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = [pool.submit(scrape, client, url, fetcher="chrome") for url in urls]
        threading.Timer(0.5, chrome.hold.set).start()
        statuses = [r.result()[0] for r in results]

    assert statuses == [200] * 5
    assert chrome.most_open_pages == 2


def test_the_engine_gets_the_chrome_settings_from_config(
    client: TestClient, web: FakeWeb, chrome: FakeChrome
) -> None:
    scrape(client, web.serve("/app/report", page("js_app.html")))

    (engine_config,) = chrome.configs
    defaults = load_config().chrome
    # A visible window on a virtual display, with a profile that survives restarts and
    # the home connection's timezone and locale.
    assert engine_config.headless is False
    assert engine_config.timezone == defaults.timezone == "America/New_York"
    assert engine_config.locale == defaults.locale == "en-US"
    assert engine_config.resolved_profile_dir().name == "chrome-profile"


def test_chrome_has_its_own_success_rate(client: TestClient, web: FakeWeb) -> None:
    scrape(client, web.serve("/app/report", page("js_app.html")))

    fetchers = client.get("/health/fetchers").json()["fetchers"]

    assert [(f["fetcher"], f["attempts"], f["successes"]) for f in fetchers] == [
        ("plain", 1, 0),
        ("chrome", 1, 1),
        ("paid", 0, 0),
    ]
