"""Seam 3: the Incident report. Given Canary results (pages, Site Adapter pages and
searches) and the success rates of the Fetchers, Search Providers and Site Adapters, the
health runner emits these Incidents with these fingerprints, and none for healthy runs.
It also decides whether a new version may take traffic."""

from __future__ import annotations

from scraper.config import HealthConfig
from scraper.health.canary import (
    CanaryEntry,
    CanaryReport,
    CanaryResult,
    SearchCanary,
    SearchCheck,
)
from scraper.health.incidents import Incident, incidents_for, judge_deploy
from scraper.health.rates import SuccessRate

HEALTH = HealthConfig(min_attempts=10, min_success_rate=0.5, widespread_fraction=0.5)

GOOD_URLS = [
    "https://docs.python.org/3/library/sqlite3.html",
    "https://en.wikipedia.org/wiki/Raspberry_Pi",
    "https://developer.mozilla.org/en-US/docs/Web/HTTP",
    "https://www.postgresql.org/docs/current/sql-select.html",
    "https://doc.rust-lang.org/book/ch01-00-getting-started.html",
    "https://go.dev/doc/effective_go",
]
MISSING_URL = "https://github.com/GTalksTech/definitely-not-a-repo-xyz"


def entry(url: str, expected: str = "accepted") -> CanaryEntry:
    return CanaryEntry(url=url, expected=expected)  # type: ignore[arg-type]


def got(
    canary: CanaryEntry,
    verdict: str,
    reason: str = "ok",
    status: int | None = 200,
    sample: str = "",
) -> CanaryResult:
    return CanaryResult(
        entry=canary,
        verdict=verdict,
        reason=reason,
        detail="",
        fetcher="plain",
        status_code=status,
        sample=sample,
    )


def healthy_results() -> list[CanaryResult]:
    results = [got(entry(u), "accepted", sample="Real article text.") for u in GOOD_URLS]
    results.append(got(entry(MISSING_URL, "rejected"), "rejected", "http_404", 404))
    return results


def report(results: list[CanaryResult]) -> CanaryReport:
    return CanaryReport(target="http://127.0.0.1:3002", results=results)


def replace_result(results: list[CanaryResult], url: str, new: CanaryResult) -> list[CanaryResult]:
    return [new if r.entry.url == url else r for r in results]


def rate(
    fetcher: str,
    attempts: int,
    successes: int,
    unavailable: int = 0,
    last_unavailable: str | None = None,
) -> SuccessRate:
    return SuccessRate("fetcher", fetcher, attempts, successes, unavailable, last_unavailable)


HEALTHY_RATES = [rate("plain", 40, 31), rate("paid", 6, 6)]


def fingerprints(incidents: list[Incident]) -> list[str]:
    return [i.fingerprint for i in incidents]


# ── healthy runs ───────────────────────────────────────────────────────────


def test_a_healthy_run_emits_no_incidents() -> None:
    assert incidents_for(report(healthy_results()), HEALTHY_RATES, HEALTH) == []


def test_a_fetcher_with_too_few_attempts_to_judge_emits_nothing() -> None:
    # 1 success in 4 is bad, but 4 attempts is too few to call it a slide.
    rates = [rate("plain", 4, 1), rate("paid", 0, 0)]

    assert incidents_for(report(healthy_results()), rates, HEALTH) == []


def test_a_known_bad_page_rejected_for_the_expected_reason_is_healthy() -> None:
    # The known-bad page is in healthy_results(), rejected as expected.
    results = healthy_results()
    assert [r.passed for r in results] == [True] * len(results)


# ── Canary failures ────────────────────────────────────────────────────────


def test_a_good_page_that_is_rejected_raises_an_incident_for_its_site() -> None:
    url = GOOD_URLS[0]
    results = replace_result(
        healthy_results(), url, got(entry(url), "rejected", "challenge", 403)
    )

    incidents = incidents_for(report(results), HEALTHY_RATES, HEALTH)

    assert fingerprints(incidents) == ["canary_failed:docs.python.org"]
    incident = incidents[0]
    assert incident.kind == "canary_failed"
    assert incident.effect == "pages from docs.python.org can't be fetched"
    [failure] = incident.evidence["failures"]
    assert failure["url"] == url
    assert failure["expected"] == "accepted"
    assert failure["verdict"] == "rejected"
    assert failure["reason"] == "challenge"
    assert failure["statusCode"] == 403


def test_a_good_page_that_comes_back_only_suspect_fails_the_canary() -> None:
    url = GOOD_URLS[1]
    results = replace_result(
        healthy_results(), url, got(entry(url), "suspect", "thin", sample="Only a stub.")
    )

    incidents = incidents_for(report(results), HEALTHY_RATES, HEALTH)

    assert fingerprints(incidents) == ["canary_failed:en.wikipedia.org"]
    assert incidents[0].evidence["failures"][0]["sample"] == "Only a stub."


def test_a_known_bad_page_passed_off_as_content_is_a_content_gate_incident() -> None:
    results = replace_result(
        healthy_results(),
        MISSING_URL,
        got(entry(MISSING_URL, "rejected"), "accepted", sample="Sign in to GitHub"),
    )

    incidents = incidents_for(report(results), HEALTHY_RATES, HEALTH)

    assert fingerprints(incidents) == ["canary_failed:github.com"]
    assert incidents[0].effect == "a page that isn't real content from github.com is passed off as content"
    assert incidents[0].evidence["failures"][0]["sample"] == "Sign in to GitHub"


def test_a_known_bad_page_rejected_for_a_different_reason_than_expected_fails() -> None:
    bad = CanaryEntry(url=MISSING_URL, expected="rejected", reason="http_404")
    results = replace_result(
        healthy_results(), MISSING_URL, got(bad, "rejected", "challenge", 403)
    )

    incidents = incidents_for(report(results), HEALTHY_RATES, HEALTH)

    assert fingerprints(incidents) == ["canary_failed:github.com"]


def test_two_failing_pages_on_one_site_are_one_incident() -> None:
    second = "https://docs.python.org/3/library/asyncio.html"
    results = healthy_results() + [got(entry(second), "rejected", "http_429", 429)]
    results = replace_result(
        results, GOOD_URLS[0], got(entry(GOOD_URLS[0]), "rejected", "http_429", 429)
    )

    incidents = incidents_for(report(results), HEALTHY_RATES, HEALTH)

    assert fingerprints(incidents) == ["canary_failed:docs.python.org"]
    assert [f["url"] for f in incidents[0].evidence["failures"]] == [GOOD_URLS[0], second]


def test_failing_pages_on_two_sites_are_two_incidents() -> None:
    results = healthy_results()
    for url in GOOD_URLS[:2]:
        results = replace_result(results, url, got(entry(url), "rejected", "too_short"))

    incidents = incidents_for(report(results), HEALTHY_RATES, HEALTH)

    assert sorted(fingerprints(incidents)) == [
        "canary_failed:docs.python.org",
        "canary_failed:en.wikipedia.org",
    ]


def test_half_the_canary_set_failing_is_one_widespread_incident() -> None:
    results = healthy_results()
    for url in GOOD_URLS[:4]:  # 4 of 7
        results = replace_result(results, url, got(entry(url), "rejected", "fetch_error", None))

    incidents = incidents_for(report(results), HEALTHY_RATES, HEALTH)

    assert fingerprints(incidents) == ["canary_widespread"]
    assert incidents[0].effect == "most pages can't be fetched (4 of 7 Canary checks failing)"
    assert len(incidents[0].evidence["failures"]) == 4


def test_a_service_that_does_not_answer_is_a_widespread_incident() -> None:
    results = [
        got(r.entry, "error", "service_error", None) for r in healthy_results()
    ]

    incidents = incidents_for(report(results), HEALTHY_RATES, HEALTH)

    assert fingerprints(incidents) == ["canary_widespread"]
    assert incidents[0].effect == "the service isn't answering (7 of 7 Canary checks failing)"


def test_the_same_failure_on_another_day_has_the_same_fingerprint() -> None:
    url = GOOD_URLS[0]
    monday = replace_result(healthy_results(), url, got(entry(url), "rejected", "challenge", 403))
    tuesday = replace_result(healthy_results(), url, got(entry(url), "rejected", "http_429", 429))

    assert fingerprints(incidents_for(report(monday), HEALTHY_RATES, HEALTH)) == fingerprints(
        incidents_for(report(tuesday), HEALTHY_RATES, HEALTH)
    )


# ── success rates ──────────────────────────────────────────────────────────


def test_a_fetcher_sliding_below_the_success_rate_floor_raises_an_incident() -> None:
    rates = [rate("plain", 40, 12), rate("paid", 6, 6)]

    incidents = incidents_for(report(healthy_results()), rates, HEALTH)

    assert fingerprints(incidents) == ["fetcher_degraded:plain"]
    incident = incidents[0]
    assert incident.kind == "fetcher_degraded"
    assert incident.effect == "the plain Fetcher is failing (30% of 40 attempts succeeded)"
    assert incident.evidence == {
        "fetcher": "plain",
        "attempts": 40,
        "successes": 12,
        "successRate": 0.3,
        "minSuccessRate": 0.5,
    }


def test_a_fetcher_exactly_at_the_floor_is_healthy() -> None:
    rates = [rate("plain", 40, 20)]

    assert incidents_for(report(healthy_results()), rates, HEALTH) == []


def test_the_paid_fetchers_quota_running_out_raises_an_incident() -> None:
    rates = [rate("plain", 40, 31), rate("paid", 6, 6, unavailable=3, last_unavailable="quota_exhausted")]

    incidents = incidents_for(report(healthy_results()), rates, HEALTH)

    assert fingerprints(incidents) == ["quota_exhausted:paid"]
    assert incidents[0].effect == (
        "the Paid Fetcher's quota is used up, so pages that need it fail"
    )


def test_a_quota_that_came_back_raises_nothing() -> None:
    # It ran out earlier in the window, but the latest attempt went through.
    rates = [rate("paid", 6, 6, unavailable=3, last_unavailable=None)]

    assert incidents_for(report(healthy_results()), rates, HEALTH) == []


def test_rates_alone_are_judged_when_there_is_no_canary_run() -> None:
    rates = [rate("plain", 40, 12)]

    assert fingerprints(incidents_for(None, rates, HEALTH)) == ["fetcher_degraded:plain"]


def test_canary_and_rate_incidents_come_together() -> None:
    url = GOOD_URLS[0]
    results = replace_result(healthy_results(), url, got(entry(url), "rejected", "challenge", 403))
    rates = [rate("plain", 40, 12)]

    incidents = incidents_for(report(results), rates, HEALTH)

    assert fingerprints(incidents) == ["canary_failed:docs.python.org", "fetcher_degraded:plain"]


def test_an_incident_is_written_as_one_json_object() -> None:
    url = GOOD_URLS[0]
    results = replace_result(healthy_results(), url, got(entry(url), "rejected", "challenge", 403))

    [incident] = incidents_for(report(results), HEALTHY_RATES, HEALTH)
    written = incident.to_json()

    assert written["kind"] == "canary_failed"
    assert written["fingerprint"] == "canary_failed:docs.python.org"
    assert written["effect"] == "pages from docs.python.org can't be fetched"
    assert written["evidence"]["failures"][0]["url"] == url


# ── deploys gated by the Canary Set ────────────────────────────────────────

NEW_IMAGE = "sha256:new"


def test_a_candidate_that_scores_as_well_as_the_live_service_is_promoted() -> None:
    decision = judge_deploy(report(healthy_results()), report(healthy_results()), NEW_IMAGE)

    assert decision.promote
    assert decision.incidents == []


def test_a_candidate_that_scores_better_is_promoted() -> None:
    url = GOOD_URLS[0]
    live = replace_result(healthy_results(), url, got(entry(url), "rejected", "too_short"))

    decision = judge_deploy(report(live), report(healthy_results()), NEW_IMAGE)

    assert decision.promote
    assert decision.incidents == []


def test_a_candidate_that_scores_worse_is_kept_out_with_an_incident() -> None:
    url = GOOD_URLS[2]
    candidate = replace_result(healthy_results(), url, got(entry(url), "rejected", "too_short"))

    decision = judge_deploy(report(healthy_results()), report(candidate), NEW_IMAGE)

    assert not decision.promote
    assert fingerprints(decision.incidents) == ["deploy_rejected"]
    incident = decision.incidents[0]
    assert incident.effect == "a new version was kept out: it did worse on the Canary Set (6 of 7 passing, live 7 of 7)"
    assert incident.evidence["candidateImage"] == NEW_IMAGE
    assert incident.evidence["candidatePassed"] == 6
    assert incident.evidence["livePassed"] == 7
    [regression] = incident.evidence["regressions"]
    assert regression["url"] == url
    assert regression["reason"] == "too_short"


def test_a_candidate_is_promoted_when_the_live_service_could_not_be_checked() -> None:
    # The live service is down (or predates the Canary runner): anything beats it.
    decision = judge_deploy(None, report(healthy_results()), NEW_IMAGE)

    assert decision.promote


def test_a_candidate_that_could_not_be_checked_is_kept_out() -> None:
    decision = judge_deploy(report(healthy_results()), None, NEW_IMAGE)

    assert not decision.promote
    assert fingerprints(decision.incidents) == ["deploy_rejected"]
    assert decision.incidents[0].effect == (
        "a new version was kept out: it didn't answer the Canary Set"
    )


def test_every_rejected_deploy_shares_one_fingerprint() -> None:
    url = GOOD_URLS[2]
    worse = report(replace_result(healthy_results(), url, got(entry(url), "rejected", "too_short")))

    first = judge_deploy(report(healthy_results()), worse, "sha256:one")
    second = judge_deploy(report(healthy_results()), worse, "sha256:two")

    assert fingerprints(first.incidents) == fingerprints(second.incidents)


# ── Search Providers and Site Adapters ─────────────────────────────────────

PROVIDERS = ["tinyfish", "exa", "tavily", "duckduckgo"]
QUERIES = [
    SearchCanary("python sqlite3 module documentation", "docs.python.org"),
    SearchCanary("git rebase documentation", "git-scm.com"),
]
ADAPTER_URL = "https://www.youtube.com/@RaspberryPi"


def searched(
    canary: SearchCanary, provider: str, outcome: str = "found", detail: str = ""
) -> SearchCheck:
    urls = [f"https://{canary.expect}/page"] if outcome == "found" else ["https://spam.example/"]
    return SearchCheck(canary, provider, outcome, detail, urls)  # type: ignore[arg-type]


def healthy_searches() -> list[SearchCheck]:
    return [searched(q, p) for q in QUERIES for p in PROVIDERS]


def with_searches(results: list[CanaryResult], searches: list[SearchCheck]) -> CanaryReport:
    return CanaryReport(target="http://127.0.0.1:3002", results=results, searches=searches)


def adapter_entry() -> CanaryEntry:
    return CanaryEntry(url=ADAPTER_URL, expected="accepted", fetcher="youtube")


def healthy_with_adapter() -> list[CanaryResult]:
    return healthy_results() + [got(adapter_entry(), "accepted", sample="# Raspberry Pi")]


def test_a_healthy_run_with_searches_and_adapters_emits_no_incidents() -> None:
    rates = HEALTHY_RATES + [
        SuccessRate("provider", "tinyfish", 30, 29),
        SuccessRate("adapter", "github", 12, 12),
    ]

    incidents = incidents_for(with_searches(healthy_with_adapter(), healthy_searches()), rates, HEALTH)

    assert incidents == []


def test_a_search_provider_sliding_below_the_floor_raises_an_incident() -> None:
    rates = [SuccessRate("provider", "exa", 20, 4)]

    incidents = incidents_for(report(healthy_results()), rates, HEALTH)

    assert fingerprints(incidents) == ["provider_degraded:exa"]
    assert incidents[0].kind == "provider_degraded"
    assert incidents[0].effect == "the exa Search Provider is failing (20% of 20 searches succeeded)"
    assert incidents[0].evidence == {
        "provider": "exa",
        "attempts": 20,
        "successes": 4,
        "successRate": 0.2,
        "minSuccessRate": 0.5,
    }


def test_a_site_adapter_sliding_below_the_floor_raises_an_incident() -> None:
    rates = [SuccessRate("adapter", "reddit", 12, 3)]

    incidents = incidents_for(report(healthy_results()), rates, HEALTH)

    assert fingerprints(incidents) == ["adapter_degraded:reddit"]
    assert incidents[0].kind == "adapter_degraded"
    assert incidents[0].effect == "the reddit Site Adapter is failing (25% of 12 attempts succeeded)"
    assert incidents[0].evidence["adapter"] == "reddit"


def test_a_search_provider_or_adapter_with_too_few_attempts_emits_nothing() -> None:
    rates = [SuccessRate("provider", "exa", 4, 0), SuccessRate("adapter", "reddit", 3, 0)]

    assert incidents_for(report(healthy_results()), rates, HEALTH) == []


def test_a_search_providers_quota_running_out_raises_an_incident() -> None:
    rates = [SuccessRate("provider", "tavily", 8, 8, 2, "quota_exhausted")]

    incidents = incidents_for(report(healthy_results()), rates, HEALTH)

    assert fingerprints(incidents) == ["quota_exhausted:tavily"]
    assert incidents[0].effect == "the tavily Search Provider's quota is used up, so searches skip it"
    assert incidents[0].evidence["provider"] == "tavily"


def test_a_search_provider_without_a_key_raises_nothing() -> None:
    # Not every Provider needs a key set; one left out is a choice, not a breakage.
    rates = [SuccessRate("provider", "tinyfish", 0, 0, 5, "not_configured")]

    assert incidents_for(report(healthy_results()), rates, HEALTH) == []


def test_a_site_adapters_canary_page_failing_raises_an_incident_for_the_adapter() -> None:
    results = replace_result(
        healthy_with_adapter(), ADAPTER_URL, got(adapter_entry(), "rejected", "http_404", 404)
    )

    incidents = incidents_for(report(results), HEALTHY_RATES, HEALTH)

    assert fingerprints(incidents) == ["adapter_canary_failed:youtube"]
    assert incidents[0].kind == "adapter_canary_failed"
    assert incidents[0].effect == "the youtube Site Adapter can't get pages from www.youtube.com"
    [failure] = incidents[0].evidence["failures"]
    assert (failure["url"], failure["reason"]) == (ADAPTER_URL, "http_404")


def test_a_site_adapter_failing_is_apart_from_a_page_on_the_same_site() -> None:
    plain = CanaryEntry(url="https://www.youtube.com/about/", expected="accepted")
    results = healthy_results() + [
        got(adapter_entry(), "rejected", "fetch_error", None),
        got(plain, "rejected", "challenge", 403),
    ]

    incidents = incidents_for(report(results), HEALTHY_RATES, HEALTH)

    assert sorted(fingerprints(incidents)) == [
        "adapter_canary_failed:youtube",
        "canary_failed:www.youtube.com",
    ]


def test_one_providers_canary_searches_failing_raise_an_incident_for_it() -> None:
    searches = [
        searched(q, p, "missing", "no result from the site among 1") if p == "exa" else searched(q, p)
        for q in QUERIES
        for p in PROVIDERS
    ]

    incidents = incidents_for(with_searches(healthy_results(), searches), HEALTHY_RATES, HEALTH)

    assert fingerprints(incidents) == ["search_canary_failed:exa"]
    incident = incidents[0]
    assert incident.kind == "search_canary_failed"
    assert incident.effect == (
        "searches through exa don't find what they should (2 of 2 Canary searches failing)"
    )
    assert [f["query"] for f in incident.evidence["searchFailures"]] == [q.query for q in QUERIES]
    assert incident.evidence["searchFailures"][0]["results"] == ["https://spam.example/"]
    assert "failures" not in incident.evidence


def test_two_providers_failing_are_two_incidents() -> None:
    searches = healthy_searches()
    searches[0] = searched(QUERIES[0], "tinyfish", "failed", "tinyfish: unreachable")
    searches[3] = searched(QUERIES[0], "duckduckgo", "failed", "duckduckgo: rate-limited")

    incidents = incidents_for(with_searches(healthy_results(), searches), HEALTHY_RATES, HEALTH)

    assert fingerprints(incidents) == [
        "search_canary_failed:tinyfish",
        "search_canary_failed:duckduckgo",
    ]


def test_every_canary_search_failing_is_one_search_down_incident() -> None:
    searches = [searched(q, p, "failed", f"{p}: unreachable") for q in QUERIES for p in PROVIDERS]
    results = healthy_results() * 2  # enough pages passing that it isn't widespread

    incidents = incidents_for(with_searches(results, searches), HEALTHY_RATES, HEALTH)

    assert fingerprints(incidents) == ["search_down"]
    assert incidents[0].effect == "search is down (tinyfish + exa + tavily + duckduckgo failing)"
    assert len(incidents[0].evidence["searchFailures"]) == 8


def test_a_service_that_answers_nothing_is_one_widespread_incident() -> None:
    results = [got(r.entry, "error", "service_error", None) for r in healthy_results()]
    searches = [searched(q, p, "error", "refused") for q in QUERIES for p in PROVIDERS]

    incidents = incidents_for(with_searches(results, searches), HEALTHY_RATES, HEALTH)

    assert fingerprints(incidents) == ["canary_widespread"]
    assert incidents[0].effect == "the service isn't answering (15 of 15 Canary checks failing)"


def test_a_canary_search_failing_differently_another_day_has_the_same_fingerprint() -> None:
    monday = healthy_searches()
    monday[1] = searched(QUERIES[0], "exa", "missing")
    tuesday = healthy_searches()
    tuesday[5] = searched(QUERIES[1], "exa", "failed", "exa: quota exhausted")

    assert fingerprints(
        incidents_for(with_searches(healthy_results(), monday), HEALTHY_RATES, HEALTH)
    ) == fingerprints(
        incidents_for(with_searches(healthy_results(), tuesday), HEALTHY_RATES, HEALTH)
    )


def test_checks_only_one_side_made_do_not_decide_a_deploy() -> None:
    # The candidate asks a Provider the live version doesn't have: its extra passes
    # mustn't make up for a page it broke.
    extra = [searched(q, "brave") for q in QUERIES]
    candidate = replace_result(
        healthy_results(), GOOD_URLS[0], got(entry(GOOD_URLS[0]), "rejected", "too_short")
    )

    decision = judge_deploy(
        with_searches(healthy_results(), healthy_searches()),
        with_searches(candidate, healthy_searches() + extra),
        NEW_IMAGE,
    )

    assert not decision.promote
    assert decision.incidents[0].evidence["total"] == 15


def test_a_live_version_that_checked_search_differently_still_compares_on_pages() -> None:
    # An older live version can't be asked Provider by Provider: its searches went
    # through the whole chain, which the candidate's per-Provider checks don't match.
    chain = [searched(q, None) for q in QUERIES]  # type: ignore[arg-type]

    decision = judge_deploy(
        with_searches(healthy_results(), chain),
        with_searches(healthy_results(), healthy_searches()),
        NEW_IMAGE,
    )

    assert decision.promote
    assert decision.summary == "promoting sha256:new: 7 of 7 passing, live 7 of 7"


def test_a_candidate_whose_searches_regress_is_kept_out() -> None:
    worse = healthy_searches()
    worse[2] = searched(QUERIES[0], "tavily", "failed", "tavily: answered HTTP 500")

    decision = judge_deploy(
        with_searches(healthy_results(), healthy_searches()),
        with_searches(healthy_results(), worse),
        NEW_IMAGE,
    )

    assert not decision.promote
    evidence = decision.incidents[0].evidence
    assert (evidence["candidatePassed"], evidence["livePassed"], evidence["total"]) == (14, 15, 15)
    [regression] = evidence["regressions"]
    assert (regression["provider"], regression["query"]) == ("tavily", QUERIES[0].query)
