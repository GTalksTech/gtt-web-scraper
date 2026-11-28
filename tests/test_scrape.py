"""Seam 1: the Firecrawl-compatible scrape endpoint, driven against the local fake web."""

from __future__ import annotations

from fastapi.testclient import TestClient

from .fakeweb import FakeWeb, page


def scrape(client: TestClient, url: str, **extra: object) -> tuple[int, dict]:  # type: ignore[type-arg]
    response = client.post("/v1/scrape", json={"url": url, "formats": ["markdown"], **extra})
    return response.status_code, response.json()


def test_real_article_comes_back_as_markdown_from_plain_download(
    client: TestClient, web: FakeWeb
) -> None:
    url = web.serve("/article", page("article.html"))

    status, body = scrape(client, url)

    assert status == 200
    assert body["success"] is True
    data = body["data"]
    assert "Raspberry Pi 5 runs the" in data["markdown"]
    assert "## What we saw" in data["markdown"]
    assert "All rights reserved" not in data["markdown"]
    assert data["metadata"]["title"].startswith("How Raspberry Pi 5 Handles Sustained Workloads")
    assert data["metadata"]["sourceURL"] == url
    assert data["metadata"]["statusCode"] == 200
    assert data["fetcher"] == "plain"
    assert data["verdict"] == "accepted"
    assert data["confidence"] == "accepted"
    assert data["publishedDate"] == "2026-10-14"


def test_thin_real_page_is_returned_flagged_possibly_partial(
    client: TestClient, web: FakeWeb
) -> None:
    url = web.serve("/changelog/4.2", page("short_note.html"))

    status, body = scrape(client, url)

    assert status == 200
    assert body["success"] is True
    assert "arm64 container images" in body["data"]["markdown"]
    assert body["data"]["verdict"] == "suspect"
    assert body["data"]["confidence"] == "possibly_partial"


def test_plain_download_sends_chrome_headers(client: TestClient, web: FakeWeb) -> None:
    # Only headers are visible over the fake web's plain HTTP; the TLS and HTTP/2
    # fingerprint is curl_cffi's job and shows up in the Canary Set instead.
    url = web.serve("/article", page("article.html"))

    scrape(client, url)

    seen = web.requests[-1].headers
    assert "Chrome/" in seen["user-agent"]
    assert "python" not in seen["user-agent"].lower()
    assert "sec-ch-ua" in seen
    assert seen["accept"].startswith("text/html")


CF_HEADERS = {"cf-mitigated": "challenge", "server": "cloudflare"}


def assert_failure(status: int, body: dict, reason: str) -> None:  # type: ignore[type-arg]
    """The plain download was rejected for `reason`, escalated to the Chrome Fetcher
    (rejected too, or skipped for the cool-down a block starts) and then to the Paid
    Fetcher (which the fake Firecrawl, not set up for these URLs, couldn't help), and
    the Caller got an explicit failure rather than content."""
    assert status == 502
    assert body["success"] is False
    assert "data" not in body
    plain, chrome, paid = body["attempts"]
    assert plain == {
        "fetcher": "plain",
        "verdict": "rejected",
        "reason": reason,
        "detail": plain["detail"],
        "statusCode": plain["statusCode"],
    }
    assert (chrome["fetcher"], chrome["verdict"]) == ("chrome", "rejected")
    assert (paid["fetcher"], paid["verdict"]) == ("paid", "rejected")
    assert f"plain: rejected ({reason}); chrome: rejected" in body["error"]
    assert "; paid: rejected" in body["error"]


def test_cloudflare_challenge_is_polled_then_fails_naming_the_verdict(
    client: TestClient, web: FakeWeb
) -> None:
    url = web.serve("/news", page("cf_challenge.html", 403, CF_HEADERS))

    status, body = scrape(client, url)

    assert_failure(status, body, "challenge")
    assert web.hits("/news") == 1 + 3  # first fetch plus three polls


def test_non_interactive_challenge_that_clears_while_polling_returns_content(
    client: TestClient, web: FakeWeb
) -> None:
    url = web.serve(
        "/news", page("cf_challenge.html", 403, CF_HEADERS), page("article.html")
    )

    status, body = scrape(client, url)

    assert status == 200
    assert body["data"]["verdict"] == "accepted"
    assert "Raspberry Pi 5 runs the" in body["data"]["markdown"]
    assert web.hits("/news") == 2


def test_turnstile_is_rejected_immediately_without_polling(
    client: TestClient, web: FakeWeb
) -> None:
    url = web.serve("/shop", page("cf_turnstile.html", 403, CF_HEADERS))

    status, body = scrape(client, url)

    assert_failure(status, body, "interactive_challenge")
    assert web.hits("/shop") == 1


def test_cookie_wall_fails_instead_of_returning_consent_text(
    client: TestClient, web: FakeWeb
) -> None:
    url = web.serve("/story", page("cookie_wall.html"))

    status, body = scrape(client, url)

    assert_failure(status, body, "cookie_wall")


def test_empty_app_shell_fails_instead_of_returning_nothing(
    client: TestClient, web: FakeWeb
) -> None:
    url = web.serve("/reports/q3", page("empty_shell.html"))

    status, body = scrape(client, url)

    assert_failure(status, body, "empty_shell")


def test_soft_404_on_a_200_fails_instead_of_returning_the_error_page(
    client: TestClient, web: FakeWeb
) -> None:
    url = web.serve("/guides/old-post", page("soft_404.html"))

    status, body = scrape(client, url)

    assert_failure(status, body, "soft_404")


def test_real_404_status_fails(client: TestClient, web: FakeWeb) -> None:
    url = web.serve("/gone", page("soft_404.html", 404))

    status, body = scrape(client, url)

    assert_failure(status, body, "http_404")
    assert body["attempts"][0]["statusCode"] == 404


def test_unreachable_site_fails_naming_the_fetch_error(client: TestClient) -> None:
    status, body = scrape(client, "http://127.0.0.1:9/nothing-listens-here")

    assert_failure(status, body, "fetch_error")
