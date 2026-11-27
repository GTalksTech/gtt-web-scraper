"""Seam 3: the Incident report. Given Canary results and per-Fetcher success rates, the
health runner emits these Incidents with these fingerprints, and none for healthy runs.
It also decides whether a new version may take traffic."""

from __future__ import annotations

from scraper.config import HealthConfig
from scraper.health.canary import CanaryEntry, CanaryReport, CanaryResult
from scraper.health.incidents import Incident, incidents_for, judge_deploy
from scraper.health.rates import FetcherRate

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
) -> FetcherRate:
    return FetcherRate(fetcher, attempts, successes, unavailable, last_unavailable)


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
    assert incidents[0].effect == "most pages can't be fetched (4 of 7 Canary pages failing)"
    assert len(incidents[0].evidence["failures"]) == 4


def test_a_service_that_does_not_answer_is_a_widespread_incident() -> None:
    results = [
        got(r.entry, "error", "service_error", None) for r in healthy_results()
    ]

    incidents = incidents_for(report(results), HEALTHY_RATES, HEALTH)

    assert fingerprints(incidents) == ["canary_widespread"]
    assert incidents[0].effect == "the service isn't answering (7 of 7 Canary pages failing)"


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
