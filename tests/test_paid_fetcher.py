"""Seam 1: Escalation to the Paid Fetcher, and what happens when its quota runs out.

Driven through the Firecrawl-compatible scrape endpoint, against the local fake web
(for the plain download) and a fake Firecrawl (for the Paid Fetcher).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from scraper.api import create_app
from scraper.config import Config

from .fakefirecrawl import FakeFirecrawl, firecrawl_page
from .fakeweb import FakeWeb, page
from .test_scrape import CF_HEADERS, scrape

# Firecrawl's own markdown for tests/fixtures/pages/article.html.
ARTICLE_MARKDOWN = """# How Raspberry Pi 5 Handles Sustained Workloads

By Sam Example, 14 October 2026

Small single-board computers are often judged by short benchmarks that finish before the
chip has had time to warm up. We wanted to know what happens when a Raspberry Pi 5 runs the
same job for a full day, so we left one compiling a large codebase in a loop for twenty-four
hours and logged its temperature, clock speed and throughput every ten seconds.

## The setup

The board sat in a plain plastic case on a desk at room temperature, with the official
active cooler fitted and the stock power supply. Nothing was overclocked. The storage was an
NVMe drive on the PCIe adapter, which removes the microSD card as a bottleneck and makes the
processor the limiting factor for the whole run.

## What we saw

Temperatures settled within twenty minutes and stayed there. The cooler's fan ramped up and
down in short bursts, and the clock never dropped below its rated speed, so throughput at hour
twenty-three matched throughput at hour one to within the noise of the measurement.
"""

# Firecrawl's own markdown for tests/fixtures/pages/short_note.html.
SHORT_NOTE_MARKDOWN = """# Release notes: tool 4.2

Version 4.2 adds support for arm64 container images and fixes a crash when the config
file is empty. The minimum supported Python version is now 3.12. Upgrading from 4.1 needs
no changes to existing config files.
"""

# Firecrawl's own markdown for tests/fixtures/pages/cookie_wall.html.
COOKIE_WALL_MARKDOWN = """# Before you continue to Example Daily

We value your privacy. Example Daily and our 143 partners use cookies and similar
technologies to store and access information on your device.

You can choose to accept all cookies, reject all non-essential cookies, or manage your
cookie preferences at any time by selecting Privacy settings at the bottom of any page.

Reject all | Manage cookie preferences | Accept all
"""


def tried(body: dict) -> list[tuple[str, str, str]]:  # type: ignore[type-arg]
    return [(a["fetcher"], a["verdict"], a["reason"]) for a in body["attempts"]]


def test_rejected_plain_download_escalates_to_the_paid_fetcher(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    url = web.serve("/news", page("cf_turnstile.html", 403, CF_HEADERS))
    firecrawl.serve(url, firecrawl_page("article.html", ARTICLE_MARKDOWN))

    status, body = scrape(client, url)

    assert status == 200
    data = body["data"]
    assert data["fetcher"] == "paid"
    assert data["verdict"] == "accepted"
    assert data["confidence"] == "accepted"
    assert "Raspberry Pi 5 runs the" in data["markdown"]
    assert data["metadata"]["title"].startswith("How Raspberry Pi 5 Handles Sustained Workloads")
    assert data["metadata"]["sourceURL"] == url
    assert data["metadata"]["statusCode"] == 200
    assert data["publishedDate"] == "2026-10-14"
    # The block put the site on a cool-down, which keeps Chrome off it too.
    assert tried(data) == [
        ("plain", "rejected", "interactive_challenge"),
        ("chrome", "rejected", "cooling_down"),
        ("paid", "accepted", "ok"),
    ]
    assert len(firecrawl.scrapes) == 1
    assert firecrawl.scrapes[0].body["url"] == url


def test_exhausted_quota_fails_explicitly_and_lists_every_fetcher_tried(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    url = web.serve("/news", page("cf_turnstile.html", 403, CF_HEADERS))
    firecrawl.serve(url, firecrawl_page("article.html", ARTICLE_MARKDOWN))
    firecrawl.quota_exhausted = True

    status, body = scrape(client, url)

    assert status == 502
    assert body["success"] is False
    assert "data" not in body
    assert body["error"].startswith("chrome skipped: 127.0.0.1 is on a cool-down")
    assert "Paid Fetcher unavailable: quota exhausted. No Fetcher produced" in body["error"]
    assert "plain: rejected (interactive_challenge)" in body["error"]
    assert tried(body) == [
        ("plain", "rejected", "interactive_challenge"),
        ("chrome", "rejected", "cooling_down"),
        ("paid", "rejected", "quota_exhausted"),
    ]
    assert body["attempts"][2]["detail"] == "Paid Fetcher unavailable: quota exhausted"


def test_once_quota_is_exhausted_the_paid_fetcher_is_unavailable(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    first = web.serve("/news", page("cf_turnstile.html", 403, CF_HEADERS))
    # Another site: the first one is now on a cool-down for the plain download.
    second = web.serve("/other", page("soft_404.html"), host="localhost")
    firecrawl.serve(second, firecrawl_page("article.html", ARTICLE_MARKDOWN))
    firecrawl.quota_exhausted = True
    scrape(client, first)

    status, body = scrape(client, second)

    assert status == 502
    assert body["error"].startswith("Paid Fetcher unavailable: quota exhausted")
    assert tried(body) == [
        ("plain", "rejected", "soft_404"),
        ("chrome", "rejected", "soft_404"),
        ("paid", "rejected", "quota_exhausted"),
    ]
    assert len(firecrawl.scrapes) == 1  # the second request never reached Firecrawl


def test_requests_that_dont_need_the_paid_fetcher_still_work_after_quota_runs_out(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    blocked = web.serve("/news", page("cf_turnstile.html", 403, CF_HEADERS))
    # Another site: the blocked one is now on a cool-down for the plain download.
    article = web.serve("/article", page("article.html"), host="localhost")
    firecrawl.quota_exhausted = True
    scrape(client, blocked)

    status, body = scrape(client, article)

    assert status == 200
    assert body["data"]["fetcher"] == "plain"


def test_paid_fetcher_checks_the_quota_again_after_the_recheck_interval(
    config: Config, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    config.paid.quota_recheck_s = 0.0
    client = TestClient(create_app(config))
    url = web.serve("/news", page("cf_turnstile.html", 403, CF_HEADERS))
    firecrawl.serve(url, firecrawl_page("article.html", ARTICLE_MARKDOWN))
    firecrawl.quota_exhausted = True
    scrape(client, url)
    firecrawl.quota_exhausted = False  # the monthly reset

    status, body = scrape(client, url)

    assert status == 200
    assert body["data"]["fetcher"] == "paid"


def test_paid_fetcher_without_an_api_key_is_unavailable(
    config: Config, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    config.paid.api_key = None
    client = TestClient(create_app(config))
    url = web.serve("/news", page("cf_turnstile.html", 403, CF_HEADERS))

    status, body = scrape(client, url)

    assert status == 502
    assert "Paid Fetcher unavailable: FIRECRAWL_API_KEY is not set" in body["error"]
    assert tried(body)[2] == ("paid", "rejected", "not_configured")
    assert firecrawl.scrapes == []


def test_suspect_paid_result_is_returned_flagged_possibly_partial(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    url = web.serve("/changelog/4.2", page("empty_shell.html"))
    firecrawl.serve(url, firecrawl_page("short_note.html", SHORT_NOTE_MARKDOWN))

    status, body = scrape(client, url)

    assert status == 200
    data = body["data"]
    assert "arm64 container images" in data["markdown"]
    assert data["fetcher"] == "paid"
    assert data["verdict"] == "suspect"
    assert data["confidence"] == "possibly_partial"
    assert tried(data) == [
        ("plain", "rejected", "empty_shell"),
        ("chrome", "rejected", "empty_shell"),
        ("paid", "suspect", "thin"),
    ]


def test_accepted_plain_download_never_spends_paid_quota(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    url = web.serve("/article", page("article.html"))

    status, body = scrape(client, url)

    assert status == 200
    assert body["data"]["fetcher"] == "plain"
    assert firecrawl.scrapes == []


def test_paid_fetcher_result_also_passes_the_content_gate(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    url = web.serve("/story", page("cookie_wall.html"))
    firecrawl.serve(url, firecrawl_page("cookie_wall.html", COOKIE_WALL_MARKDOWN))

    status, body = scrape(client, url)

    assert status == 502
    assert body["success"] is False
    assert "data" not in body
    assert tried(body) == [
        ("plain", "rejected", "cookie_wall"),
        ("chrome", "rejected", "cookie_wall"),
        ("paid", "rejected", "cookie_wall"),
    ]
    assert "chrome: rejected (cookie_wall); paid: rejected (cookie_wall)" in body["error"]


def test_paid_fetcher_challenge_is_rejected_without_spending_quota_on_polls(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    url = web.serve("/news", page("cf_challenge.html", 403, CF_HEADERS))
    firecrawl.serve(url, firecrawl_page("cf_challenge.html", "Just a moment...", status=403))

    status, body = scrape(client, url)

    assert status == 502
    assert tried(body) == [
        ("plain", "rejected", "challenge"),
        ("chrome", "rejected", "cooling_down"),
        ("paid", "rejected", "challenge"),
    ]
    assert len(firecrawl.scrapes) == 1
