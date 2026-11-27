"""Seam 1: health tracking as seen from outside. Rolling success rates per Fetcher,
read from the service's /health/fetchers endpoint after real requests, and the Canary
runner asking a running service for each Canary URL, against the local fake web and
fake Firecrawl.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scraper.api import create_app
from scraper.config import CANARY_SET_FILE, DEFAULT_PATH, Config
from scraper.health.__main__ import health_run, write_incidents
from scraper.health.canary import CanaryEntry, load_canary_set, run_canary

from .fakefirecrawl import FakeFirecrawl, firecrawl_page
from .fakeweb import FakeWeb, page
from .test_cache import FakeClock
from .test_paid_fetcher import ARTICLE_MARKDOWN
from .test_scrape import CF_HEADERS, scrape

DAY_S = 24 * 60 * 60


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(config: Config, clock: FakeClock) -> TestClient:
    config.health.window_s = DAY_S
    return TestClient(create_app(config, clock=clock))


class ClientService:
    """The service under test, spoken to over its HTTP API as the runner would."""

    def __init__(self, client: TestClient) -> None:
        self._client = client

    @property
    def name(self) -> str:
        return "test-service"

    def post(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        response = self._client.post(path, json=body)
        return response.status_code, response.json()

    def get(self, path: str) -> tuple[int, dict[str, Any]]:
        response = self._client.get(path)
        return response.status_code, response.json()


def rates(client: TestClient) -> dict[str, dict[str, Any]]:
    response = client.get("/health/fetchers")
    assert response.status_code == 200
    return {r["fetcher"]: r for r in response.json()["fetchers"]}


# ── rolling success rates per Fetcher ──────────────────────────────────────


def test_a_new_service_has_no_attempts_for_any_fetcher(client: TestClient) -> None:
    assert rates(client) == {
        "plain": {
            "fetcher": "plain",
            "attempts": 0,
            "successes": 0,
            "successRate": None,
            "unavailable": 0,
            "lastUnavailable": None,
        },
        "paid": {
            "fetcher": "paid",
            "attempts": 0,
            "successes": 0,
            "successRate": None,
            "unavailable": 0,
            "lastUnavailable": None,
        },
    }


def test_each_fetchers_attempts_and_successes_are_counted(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    article = web.serve("/article", page("article.html"))
    blocked = web.serve("/blocked", page("cf_turnstile.html", 403, CF_HEADERS), host="localhost")
    firecrawl.serve(blocked, firecrawl_page("article.html", ARTICLE_MARKDOWN))
    scrape(client, article)  # plain: accepted
    scrape(client, blocked)  # plain: rejected, then paid: accepted

    seen = rates(client)

    assert (seen["plain"]["attempts"], seen["plain"]["successes"]) == (2, 1)
    assert seen["plain"]["successRate"] == 0.5
    assert (seen["paid"]["attempts"], seen["paid"]["successes"]) == (1, 1)


def test_a_suspect_result_counts_as_a_success(client: TestClient, web: FakeWeb) -> None:
    scrape(client, web.serve("/changelog", page("short_note.html")))

    assert rates(client)["plain"]["successes"] == 1


def test_a_page_that_does_not_exist_is_not_held_against_the_fetcher(
    config: Config, clock: FakeClock, web: FakeWeb
) -> None:
    # The Fetcher got the site's real answer; the page just isn't there. The Canary
    # Set's known-bad pages are like this every day.
    config.ladder = ["plain"]
    client = TestClient(create_app(config, clock=clock))
    scrape(client, web.serve("/missing", page("soft_404.html", 404)))
    scrape(client, web.serve("/gone", page("soft_404.html", 410)))

    assert rates(client)["plain"]["attempts"] == 0


def test_a_cache_hit_is_not_an_attempt(client: TestClient, web: FakeWeb) -> None:
    url = web.serve("/article", page("article.html"))
    scrape(client, url)
    scrape(client, url)

    assert rates(client)["plain"]["attempts"] == 1


def test_a_fetcher_skipped_for_a_cool_down_is_not_counted(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    blocked = web.serve("/news", page("cf_turnstile.html", 403, CF_HEADERS))
    later = web.serve("/sport", page("article.html"))
    firecrawl.serve(blocked, firecrawl_page("article.html", ARTICLE_MARKDOWN))
    firecrawl.serve(later, firecrawl_page("article.html", ARTICLE_MARKDOWN))
    scrape(client, blocked)
    scrape(client, later)  # plain skipped: the site is cooling down

    assert rates(client)["plain"]["attempts"] == 1


def test_an_exhausted_quota_is_counted_apart_and_shows_as_the_latest(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    firecrawl.quota_exhausted = True
    scrape(client, web.serve("/blocked", page("cf_turnstile.html", 403, CF_HEADERS)))

    paid = rates(client)["paid"]

    assert paid["attempts"] == 0
    assert paid["unavailable"] == 1
    assert paid["lastUnavailable"] == "quota_exhausted"


def test_attempts_older_than_the_window_drop_out(
    client: TestClient, web: FakeWeb, clock: FakeClock
) -> None:
    scrape(client, web.serve("/article", page("article.html")))
    clock.advance(DAY_S + 60)
    scrape(client, web.serve("/changelog", page("short_note.html")))

    assert rates(client)["plain"]["attempts"] == 1


def test_rates_survive_a_restart(config: Config, client: TestClient, web: FakeWeb, clock: FakeClock) -> None:
    scrape(client, web.serve("/article", page("article.html")))

    restarted = TestClient(create_app(config, clock=clock))

    assert rates(restarted)["plain"]["attempts"] == 1


# ── the Canary runner ──────────────────────────────────────────────────────


def test_the_canary_set_is_asked_of_the_service_with_a_live_fetch(
    client: TestClient, web: FakeWeb
) -> None:
    url = web.serve("/article", page("article.html"))
    scrape(client, url)  # cached: the Canary must not be fooled by it
    entries = [CanaryEntry(url=url, expected="accepted")]

    report = run_canary(entries, ClientService(client))

    assert web.hits("/article") == 2
    [result] = report.results
    assert result.passed
    assert (result.verdict, result.fetcher, result.status_code) == ("accepted", "plain", 200)
    assert "Raspberry Pi 5 runs the" in result.sample


def test_a_canary_known_bad_page_passes_when_rejected_for_the_expected_reason(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    url = web.serve("/missing", page("soft_404.html", 404))
    entries = [CanaryEntry(url=url, expected="rejected", reason="http_404")]

    report = run_canary(entries, ClientService(client))

    [result] = report.results
    assert result.passed
    assert (result.verdict, result.reason, result.status_code) == ("rejected", "http_404", 404)
    # Forced to the plain download: a known-bad page never spends the Paid Fetcher's quota.
    assert firecrawl.scrapes == []


def test_a_canary_page_that_is_blocked_fails_with_the_evidence(
    client: TestClient, web: FakeWeb
) -> None:
    url = web.serve("/news", page("cf_challenge.html", 403, CF_HEADERS))
    entries = [CanaryEntry(url=url, expected="accepted")]

    report = run_canary(entries, ClientService(client))

    [failure] = report.failures
    assert (failure.verdict, failure.reason, failure.fetcher) == ("rejected", "challenge", "plain")
    assert failure.status_code == 403
    assert "cf-mitigated" in failure.detail


def test_a_service_that_refuses_the_request_is_a_failed_canary(
    client: TestClient, web: FakeWeb
) -> None:
    url = web.serve("/article", page("article.html"))
    entries = [CanaryEntry(url=url, expected="accepted", fetcher="no-such-fetcher")]

    [result] = run_canary(entries, ClientService(client)).results

    assert not result.passed
    assert (result.verdict, result.reason) == ("error", "service_error")
    assert "HTTP 422" in result.detail


def test_a_health_run_emits_incidents_from_the_canary_set_and_the_rates(
    client: TestClient, web: FakeWeb
) -> None:
    good = web.serve("/article", page("article.html"))
    bad = web.serve("/news", page("cf_challenge.html", 403, CF_HEADERS), host="localhost")
    entries = [
        CanaryEntry(url=good, expected="accepted"),
        CanaryEntry(url=bad, expected="accepted"),
        CanaryEntry(url=web.serve("/more", page("article.html")), expected="accepted"),
    ]

    incidents = health_run(entries, ClientService(client), Config().health)

    assert [i.fingerprint for i in incidents] == ["canary_failed:localhost"]


def test_a_healthy_health_run_emits_nothing(client: TestClient, web: FakeWeb) -> None:
    entries = [CanaryEntry(url=web.serve("/article", page("article.html")), expected="accepted")]

    assert health_run(entries, ClientService(client), Config().health) == []


# ── the seeded Canary Set ──────────────────────────────────────────────────


def test_the_seeded_canary_set_has_20_to_30_entries_with_some_known_bad_pages() -> None:
    entries = load_canary_set(DEFAULT_PATH.parent / CANARY_SET_FILE)

    assert 20 <= len(entries) <= 30
    assert sum(e.expected == "rejected" for e in entries) >= 3
    assert sum(e.expected == "accepted" for e in entries) >= 15
    # Never through the Paid Fetcher: a daily run must not spend its quota.
    assert all(e.fetcher == "plain" for e in entries)


@pytest.mark.parametrize(
    "yaml_text, problem",
    [
        ("- url: https://a.example/\n", "expected Verdict"),
        ("- url: https://a.example/\n  expected: fine\n", "expected must be one of"),
        ("- url: not a url\n  expected: accepted\n", "is not a URL"),
        (
            "- {url: 'https://a.example/', expected: accepted}\n"
            "- {url: 'https://a.example/', expected: accepted}\n",
            "listed twice",
        ),
    ],
)
def test_a_malformed_canary_set_is_refused(tmp_path: Path, yaml_text: str, problem: str) -> None:
    path = tmp_path / "canary.yaml"
    path.write_text(yaml_text)

    with pytest.raises(ValueError, match=problem):
        load_canary_set(path)


def test_incidents_are_written_one_json_object_per_line(
    client: TestClient, web: FakeWeb, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = web.serve("/news", page("cf_challenge.html", 403, CF_HEADERS))
    entries = [CanaryEntry(url=bad, expected="accepted")]

    write_incidents(health_run(entries, ClientService(client), Config().health))

    [line] = capsys.readouterr().out.splitlines()
    assert json.loads(line)["fingerprint"] == "canary_failed:127.0.0.1"
