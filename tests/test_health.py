"""Seam 1: health tracking as seen from outside. Rolling success rates per Fetcher and
Search Provider, read from the service's /health endpoints after real requests, and the
Canary runner asking a running service for each Canary URL and search, against the
local fake web, fake Firecrawl and fake Search Providers. (Site Adapters' rates are in
test_site_adapters.py, beside their fake APIs.)
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scraper.api import create_app
from scraper.config import CANARY_SET_FILE, DEFAULT_PATH, Config, load_config
from scraper.health.__main__ import deploy_check, health_run, write_incidents
from scraper.health.canary import CanaryEntry, CanarySet, SearchCanary, load_canary_set, run_canary

from .fakefirecrawl import FakeFirecrawl, firecrawl_page
from .fakesearch import PROVIDERS, FakeSearchProviders, Hit, hits
from .fakeweb import FakeResponse, FakeWeb, page
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


def test_fetcher_rates_kept_by_the_previous_version_carry_over(
    config: Config, clock: FakeClock
) -> None:
    # The version before Search Provider and Site Adapter rates kept Fetchers' alone.
    with sqlite3.connect(config.cache.path) as db:
        db.execute(
            "CREATE TABLE fetcher_outcomes"
            " (fetcher TEXT NOT NULL, at REAL NOT NULL, outcome TEXT NOT NULL, reason TEXT)"
        )
        db.execute(
            "INSERT INTO fetcher_outcomes VALUES ('plain', ?, 'failure', 'challenge')",
            (clock(),),
        )

    client = TestClient(create_app(config, clock=clock))

    assert (rates(client)["plain"]["attempts"], rates(client)["plain"]["successes"]) == (1, 0)
    # Read once: a second start doesn't count it again.
    assert rates(TestClient(create_app(config, clock=clock)))["plain"]["attempts"] == 1


# ── rolling success rates per Search Provider ──────────────────────────────


def provider_rates(client: TestClient) -> dict[str, dict[str, Any]]:
    response = client.get("/health/providers")
    assert response.status_code == 200
    return {r["provider"]: r for r in response.json()["providers"]}


def search(client: TestClient, query: str, **extra: Any) -> Any:
    return client.post("/v1/search", json={"query": query, **extra})


def test_each_search_providers_attempts_and_successes_are_counted(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    providers.fail("tinyfish")
    search(client, "raspberry pi 5")  # tinyfish fails, exa answers
    providers.answer("exa", [])
    search(client, "pi zero")  # tinyfish fails, exa finds nothing, tavily answers

    seen = provider_rates(client)

    assert list(seen) == ["tinyfish", "exa", "tavily", "duckduckgo"]
    assert (seen["tinyfish"]["attempts"], seen["tinyfish"]["successes"]) == (2, 0)
    assert (seen["exa"]["attempts"], seen["exa"]["successes"]) == (2, 1)
    assert (seen["tavily"]["attempts"], seen["tavily"]["successes"]) == (1, 1)
    assert seen["duckduckgo"]["attempts"] == 0


def test_a_search_providers_exhausted_quota_is_counted_apart(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    providers.fail("tinyfish")
    providers.run_out("exa")  # Exa says so with a 402
    search(client, "raspberry pi 5")

    exa = provider_rates(client)["exa"]

    assert (exa["attempts"], exa["unavailable"]) == (0, 1)
    assert exa["lastUnavailable"] == "quota_exhausted"


def test_a_search_provider_that_rate_limits_is_failing_not_out_of_quota(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    # A 429 can be a per-minute limit: one burst mustn't read as a used-up quota.
    providers.run_out("tinyfish")  # TinyFish says so with a 429
    search(client, "raspberry pi 5")

    tinyfish = provider_rates(client)["tinyfish"]

    assert (tinyfish["attempts"], tinyfish["successes"], tinyfish["unavailable"]) == (1, 0, 0)


def test_a_search_provider_without_a_key_is_unavailable_not_failing(
    config: Config, clock: FakeClock
) -> None:
    config.search.exa_api_key = None
    config.search.providers = ["exa", "tavily"]
    client = TestClient(create_app(config, clock=clock))
    search(client, "raspberry pi 5")

    exa = provider_rates(client)["exa"]

    assert (exa["attempts"], exa["lastUnavailable"]) == (0, "not_configured")


def test_a_search_served_from_the_cache_is_not_an_attempt(client: TestClient) -> None:
    search(client, "raspberry pi 5")
    search(client, "raspberry pi 5")

    assert provider_rates(client)["tinyfish"]["attempts"] == 1


def test_a_search_can_be_forced_to_one_provider_with_a_live_search(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    search(client, "raspberry pi 5")  # tinyfish answers, and it's cached

    response = search(client, "raspberry pi 5", provider="tavily")

    assert response.status_code == 200
    assert response.json()["provider"] == "tavily"
    assert providers.calls() == ["tinyfish", "tavily"]
    assert provider_rates(client)["tavily"]["successes"] == 1


def test_a_forced_provider_that_fails_does_not_pass_the_query_on(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    providers.fail("exa")

    response = search(client, "raspberry pi 5", provider="exa")

    assert response.status_code == 502
    assert providers.calls() == ["exa"]


def test_forcing_an_unknown_provider_is_refused(client: TestClient) -> None:
    response = search(client, "raspberry pi 5", provider="google")

    assert response.status_code == 422
    assert response.json()["code"] == "UNKNOWN_PROVIDER"


# ── the Canary runner ──────────────────────────────────────────────────────


def test_the_canary_set_is_asked_of_the_service_with_a_live_fetch(
    client: TestClient, web: FakeWeb
) -> None:
    url = web.serve("/article", page("article.html"))
    scrape(client, url)  # cached: the Canary must not be fooled by it
    entries = [CanaryEntry(url=url, expected="accepted")]

    report = run_canary(CanarySet(entries), ClientService(client))

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

    report = run_canary(CanarySet(entries), ClientService(client))

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

    report = run_canary(CanarySet(entries), ClientService(client))

    [failure] = report.failures
    assert (failure.verdict, failure.reason, failure.fetcher) == ("rejected", "challenge", "plain")
    assert failure.status_code == 403
    assert "cf-mitigated" in failure.detail


def test_a_service_that_refuses_the_request_is_a_failed_canary(
    client: TestClient, web: FakeWeb
) -> None:
    url = web.serve("/article", page("article.html"))
    entries = [CanaryEntry(url=url, expected="accepted", fetcher="no-such-fetcher")]

    [result] = run_canary(CanarySet(entries), ClientService(client)).results

    assert not result.passed
    assert (result.verdict, result.reason) == ("error", "service_error")
    assert "HTTP 422" in result.detail


DOCS = Hit("sqlite3 — DB-API 2.0", "https://docs.python.org/3/library/sqlite3.html", "…")
SQLITE = SearchCanary(query="python sqlite3 module", expect="python.org")


def test_a_canary_search_is_asked_of_every_provider_in_turn_live(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    for name in PROVIDERS:
        providers.answer(name, [DOCS])
    search(client, SQLITE.query, limit=10)  # cached: the Canary must not be fooled by it

    report = run_canary(CanarySet(searches=[SQLITE]), ClientService(client))

    assert [(s.provider, s.passed) for s in report.searches] == [(p, True) for p in PROVIDERS]
    assert [len(providers.queries[p]) for p in PROVIDERS] == [2, 1, 1, 1]


def test_a_canary_search_without_the_expected_site_fails_with_the_results(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    for name in PROVIDERS:
        providers.answer(name, [DOCS])
    providers.answer("exa", hits("exa"))

    report = run_canary(CanarySet(searches=[SQLITE]), ClientService(client))

    [failure] = report.search_failures
    assert (failure.provider, failure.outcome) == ("exa", "missing")
    assert failure.urls == [h.url for h in hits("exa")]


def test_a_canary_search_through_a_failing_provider_fails_with_its_error(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    for name in PROVIDERS:
        providers.answer(name, [DOCS])
    providers.run_out("tavily")

    report = run_canary(CanarySet(searches=[SQLITE]), ClientService(client))

    [failure] = report.search_failures
    assert (failure.provider, failure.outcome) == ("tavily", "failed")
    assert "quota exhausted" in failure.detail


def test_a_canary_search_skips_a_provider_with_no_key_set(
    config: Config, clock: FakeClock, providers: FakeSearchProviders
) -> None:
    # Leaving a Provider's key out is a choice, not a breakage: nothing to report.
    config.search.tinyfish_api_key = None
    client = TestClient(create_app(config, clock=clock))
    for name in PROVIDERS:
        providers.answer(name, [DOCS])

    report = run_canary(CanarySet(searches=[SQLITE]), ClientService(client))

    assert [s.provider for s in report.searches] == ["exa", "tavily", "duckduckgo"]
    assert report.search_failures == []


def test_a_canary_page_forced_through_a_site_adapter_is_judged_on_the_adapter_alone(
    config: Config, clock: FakeClock, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    # The adapter's API is down; the Ladder would have got the page, hiding that.
    config.adapters.github_api_url = web.url("")
    client = TestClient(create_app(config, clock=clock))
    url = "https://github.com/octo/widget"
    firecrawl.serve(url, firecrawl_page("article.html", ARTICLE_MARKDOWN))
    entries = [CanaryEntry(url=url, expected="accepted", fetcher="github")]

    [failure] = run_canary(CanarySet(entries), ClientService(client)).failures

    assert (failure.verdict, failure.fetcher, failure.reason) == ("rejected", "github", "http_404")
    assert firecrawl.scrapes == []


def test_a_health_run_reads_every_kind_of_success_rate(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    providers.fail("tinyfish")
    for i in range(10):
        search(client, f"query {i}")

    incidents = health_run(CanarySet(), ClientService(client), Config().health)

    assert [i.fingerprint for i in incidents] == ["provider_degraded:tinyfish"]


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

    incidents = health_run(CanarySet(entries), ClientService(client), Config().health)

    assert [i.fingerprint for i in incidents] == ["canary_failed:localhost"]


def test_a_healthy_health_run_emits_nothing(client: TestClient, web: FakeWeb) -> None:
    entries = [CanaryEntry(url=web.serve("/article", page("article.html")), expected="accepted")]

    assert health_run(CanarySet(entries), ClientService(client), Config().health) == []


# ── the deploy check ───────────────────────────────────────────────────────


def test_a_page_the_candidate_failed_once_is_checked_again_before_judging(
    client: TestClient, web: FakeWeb
) -> None:
    # Live gets the page, the candidate hits a blip, then the re-check gets it.
    url = web.serve(
        "/flaky", page("article.html"), FakeResponse(b"busy", 503), page("article.html")
    )
    entries = [CanaryEntry(url=url, expected="accepted")]
    service = ClientService(client)

    decision = deploy_check(CanarySet(entries), service, service, "sha256:new", wait_s=0, recheck_pause_s=0)

    assert decision.promote
    assert web.hits("/flaky") == 3


def test_a_page_the_candidate_keeps_failing_keeps_it_out(client: TestClient, web: FakeWeb) -> None:
    url = web.serve("/broken", page("article.html"), FakeResponse(b"busy", 503))
    entries = [CanaryEntry(url=url, expected="accepted")]
    service = ClientService(client)

    decision = deploy_check(CanarySet(entries), service, service, "sha256:new", wait_s=0, recheck_pause_s=0)

    assert not decision.promote
    assert [i.fingerprint for i in decision.incidents] == ["deploy_rejected"]


# ── the seeded Canary Set ──────────────────────────────────────────────────


def test_the_seeded_canary_set_has_20_to_30_pages_with_some_known_bad_ones() -> None:
    pages = load_canary_set(DEFAULT_PATH.parent / CANARY_SET_FILE).pages
    plain = [e for e in pages if e.fetcher == "plain"]

    assert 20 <= len(plain) <= 30
    assert sum(e.expected == "rejected" for e in plain) >= 3
    # Some known-bad pages come with a 200, so only the Content Gate can catch them.
    assert any(e.expected == "rejected" and e.reason != "http_404" for e in plain)
    assert sum(e.expected == "accepted" for e in plain) >= 15


def test_the_seeded_canary_set_checks_every_site_adapter_once() -> None:
    pages = load_canary_set(DEFAULT_PATH.parent / CANARY_SET_FILE).pages
    adapters = load_config().adapters.enabled

    forced = sorted(e.fetcher for e in pages if e.fetcher != "plain")

    assert forced == sorted(adapters)
    # Never through the Paid Fetcher (or the Ladder that ends in it): a daily run must
    # not spend its quota.
    assert all(e.fetcher in ["plain", *adapters] for e in pages)


def test_the_seeded_canary_set_has_searches_asked_of_every_provider() -> None:
    searches = load_canary_set(DEFAULT_PATH.parent / CANARY_SET_FILE).searches

    assert len(searches) >= 2
    assert all(s.provider is None for s in searches)


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
        ("- query: raspberry pi\n", "a query and a domain"),
        ("- query: raspberry pi\n  expect: https://raspberrypi.com/\n", "must be a domain"),
        (
            "- {query: raspberry pi, expect: raspberrypi.com}\n"
            "- {query: raspberry pi, expect: wikipedia.org}\n",
            "searches listed twice",
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

    write_incidents(health_run(CanarySet(entries), ClientService(client), Config().health))

    [line] = capsys.readouterr().out.splitlines()
    assert json.loads(line)["fingerprint"] == "canary_failed:127.0.0.1"
