"""Incidents: what the health runner reports when something degraded, with the evidence
an agent needs to fix it without reproducing it from a data-center address.

Each Incident has a kind, the effect a Caller sees, evidence, and a fingerprint. The
fingerprint names the problem, not the occurrence: the same problem found again on
another day (or failing in a different way) gets the same fingerprint, so it joins
the open Incident instead of opening a new one.

  canary_failed:<site>            Canary pages on one Site didn't get their expected Verdict
  adapter_canary_failed:<name>    a Site Adapter's Canary page didn't come back as content
  search_canary_failed:<name>     a Search Provider's Canary searches didn't find what they should
  search_down                     every Canary search failed, through every Search Provider
  canary_widespread               so much of the Canary Set failed that it's one problem
  fetcher_degraded:<name>         a Fetcher's rolling success rate fell below the floor
  provider_degraded:<name>        a Search Provider's rolling success rate fell below the floor
  adapter_degraded:<name>         a Site Adapter's rolling success rate fell below the floor
  quota_exhausted:<name>          a Fetcher or Search Provider is unavailable: its quota ran out
  deploy_rejected                 a new version did worse on the Canary Set and was kept out
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..adapters import ADAPTER_NAMES
from ..config import HealthConfig
from ..fetchers import QUOTA_EXHAUSTED
from ..sites import site_of
from .canary import CanaryReport, CanaryResult, SearchCheck
from .rates import SuccessRate


@dataclass(frozen=True)
class Incident:
    kind: str
    effect: str  # what a Caller sees, in a few words: it becomes the issue's subject
    evidence: dict[str, Any]
    fingerprint: str

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "effect": self.effect,
            "fingerprint": self.fingerprint,
            "evidence": self.evidence,
        }


def incidents_for(
    report: CanaryReport | None, rates: Sequence[SuccessRate], config: HealthConfig
) -> list[Incident]:
    """The Incidents a health run found: from its Canary results (if the Canary Set was
    run) and the rolling success rates of the Fetchers, Search Providers and Site
    Adapters. None for a healthy run."""
    incidents = _canary_incidents(report, config) if report is not None else []
    for rate in rates:
        incidents += _rate_incidents(rate, config)
    return incidents


def _canary_incidents(report: CanaryReport, config: HealthConfig) -> list[Incident]:
    pages, searches = report.failures, report.search_failures
    failing = len(pages) + len(searches)
    if not failing:
        return []
    total = report.total
    if failing >= max(2, math.ceil(config.widespread_fraction * total)):
        down = all(p.service_error for p in pages) and all(s.service_error for s in searches)
        if down:
            what = "the service isn't answering"
        elif searches:
            what = "pages and searches are failing"
        else:
            what = "most pages can't be fetched"
        return [
            Incident(
                kind="canary_widespread",
                effect=f"{what} ({failing} of {total} Canary checks failing)",
                evidence=_canary_evidence(report, pages, searches),
                fingerprint="canary_widespread",
            )
        ]
    return _page_incidents(report, pages) + _search_incidents(report, searches)


def _page_incidents(report: CanaryReport, failures: list[CanaryResult]) -> list[Incident]:
    """One Incident per Site Adapter whose page failed, and one per Site for the rest."""
    incidents = []
    by_adapter: dict[str, list[CanaryResult]] = {}
    by_site: dict[str, list[CanaryResult]] = {}
    for failure in failures:
        if failure.entry.fetcher in ADAPTER_NAMES:
            by_adapter.setdefault(failure.entry.fetcher, []).append(failure)
        else:
            by_site.setdefault(site_of(failure.entry.url), []).append(failure)
    for adapter, adapter_failures in by_adapter.items():
        sites = ", ".join(dict.fromkeys(site_of(f.entry.url) for f in adapter_failures))
        incidents.append(
            Incident(
                kind="adapter_canary_failed",
                effect=f"the {adapter} Site Adapter can't get pages from {sites}",
                evidence=_canary_evidence(report, adapter_failures, []),
                fingerprint=f"adapter_canary_failed:{adapter}",
            )
        )
    for site, site_failures in by_site.items():
        incidents.append(
            Incident(
                kind="canary_failed",
                effect=_site_effect(site, site_failures),
                evidence=_canary_evidence(report, site_failures, []),
                fingerprint=f"canary_failed:{site}",
            )
        )
    return incidents


def _site_effect(site: str, failures: list[CanaryResult]) -> str:
    if all(f.entry.expected == "rejected" for f in failures):
        return f"a page that isn't real content from {site} is passed off as content"
    return f"pages from {site} can't be fetched"


def _search_incidents(report: CanaryReport, failures: list[SearchCheck]) -> list[Incident]:
    """One Incident per Search Provider whose Canary searches failed, or one for all of
    search when every Canary search failed through every Provider asked."""
    if not failures:
        return []
    by_provider: dict[str, list[SearchCheck]] = {}
    for failure in failures:
        by_provider.setdefault(_provider_name(failure.provider), []).append(failure)
    if len(failures) == len(report.searches):
        return [
            Incident(
                kind="search_down",
                effect=f"search is down ({' + '.join(by_provider)} failing)",
                evidence=_canary_evidence(report, [], failures),
                fingerprint="search_down",
            )
        ]
    asked = {_provider_name(s.provider): 0 for s in report.searches}
    for check in report.searches:
        asked[_provider_name(check.provider)] += 1
    return [
        Incident(
            kind="search_canary_failed",
            effect=(
                f"searches through {provider} don't find what they should"
                f" ({len(provider_failures)} of {asked[provider]} Canary searches failing)"
            ),
            evidence=_canary_evidence(report, [], provider_failures),
            fingerprint=f"search_canary_failed:{provider}",
        )
        for provider, provider_failures in by_provider.items()
    ]


def _provider_name(provider: str | None) -> str:
    return provider or "chain"


def _canary_evidence(
    report: CanaryReport, pages: list[CanaryResult], searches: list[SearchCheck]
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "target": report.target,
        "passed": report.passed,
        "total": report.total,
    }
    if pages:
        evidence["failures"] = [f.evidence() for f in pages]
    if searches:
        evidence["searchFailures"] = [f.evidence() for f in searches]
    return evidence


# How each subject is named in an Incident's effect, and what its attempts are.
_TITLES = {"fetcher": "Fetcher", "provider": "Search Provider", "adapter": "Site Adapter"}
_ATTEMPTS = {"fetcher": "attempts", "provider": "searches", "adapter": "attempts"}
# What a quota running out costs a Caller (Site Adapters have none).
_QUOTA_EFFECT = {
    "fetcher": "pages that need it fail",
    "provider": "searches skip it",
}


def _rate_incidents(rate: SuccessRate, config: HealthConfig) -> list[Incident]:
    incidents = []
    name = _subject_name(rate)
    if rate.last_unavailable == QUOTA_EXHAUSTED:
        incidents.append(
            Incident(
                kind="quota_exhausted",
                effect=f"the {name}'s quota is used up, so {_QUOTA_EFFECT[rate.subject]}",
                evidence=rate.to_json(),
                fingerprint=f"quota_exhausted:{rate.name}",
            )
        )
    success_rate = rate.success_rate
    if (
        success_rate is not None
        and rate.attempts >= config.min_attempts
        and success_rate < config.min_success_rate
    ):
        incidents.append(
            Incident(
                kind=f"{rate.subject}_degraded",
                effect=(
                    f"the {name} is failing ({success_rate:.0%} of"
                    f" {rate.attempts} {_ATTEMPTS[rate.subject]} succeeded)"
                ),
                evidence={
                    rate.subject: rate.name,
                    "attempts": rate.attempts,
                    "successes": rate.successes,
                    "successRate": round(success_rate, 4),
                    "minSuccessRate": config.min_success_rate,
                },
                fingerprint=f"{rate.subject}_degraded:{rate.name}",
            )
        )
    return incidents


def _subject_name(rate: SuccessRate) -> str:
    if rate.subject == "fetcher" and rate.name == "paid":
        return "Paid Fetcher"
    return f"{rate.name} {_TITLES[rate.subject]}"


@dataclass(frozen=True)
class DeployDecision:
    promote: bool
    summary: str  # one line for the deploy log
    incidents: list[Incident] = field(default_factory=list)


def judge_deploy(
    live: CanaryReport | None, candidate: CanaryReport | None, candidate_image: str
) -> DeployDecision:
    """Whether a new version (`candidate_image`) may take traffic: only if it passes at
    least as much of the Canary Set (pages and searches) as the live service. Only the
    checks both made are compared: the two can list different Search Providers (a
    Provider added or replaced, or an older live version that searched through the
    whole chain). A live service that couldn't be checked counts as passing nothing;
    a candidate that couldn't be checked is kept out."""
    if candidate is None:
        return DeployDecision(
            promote=False,
            summary=f"kept out {candidate_image}: it didn't answer the Canary Set",
            incidents=[
                _deploy_rejected(
                    "it didn't answer the Canary Set",
                    {
                        "candidateImage": candidate_image,
                        "livePassed": live.passed if live is not None else 0,
                    },
                )
            ],
        )
    checks = candidate.checks
    if live is not None:
        shared = {c.key for c in live.checks}
        checks = [c for c in checks if c.key in shared]
    candidate_passed = sum(c.passed for c in checks)
    live_ok = (
        {c.key for c in live.checks if c.passed} & {c.key for c in checks}
        if live is not None
        else set()
    )
    live_passed = len(live_ok)
    score = f"{candidate_passed} of {len(checks)}"
    live_score = f"{live_passed} of {len(checks)}" if live is not None else "unchecked"
    if candidate_passed >= live_passed:
        return DeployDecision(
            promote=True, summary=f"promoting {candidate_image}: {score} passing, live {live_score}"
        )
    regressions = [c.evidence() for c in checks if not c.passed and c.key in live_ok]
    return DeployDecision(
        promote=False,
        summary=f"kept out {candidate_image}: {score} passing, live {live_score}",
        incidents=[
            _deploy_rejected(
                f"it did worse on the Canary Set ({score} passing, live {live_score})",
                {
                    "candidateImage": candidate_image,
                    "candidatePassed": candidate_passed,
                    "livePassed": live_passed,
                    "total": len(checks),
                    "regressions": regressions,
                },
            )
        ],
    )


def _deploy_rejected(why: str, evidence: dict[str, Any]) -> Incident:
    # One fingerprint for every rejected deploy: each new image is another attempt at
    # the same fix, so they gather on one Incident.
    return Incident(
        kind="deploy_rejected",
        effect=f"a new version was kept out: {why}",
        evidence=evidence,
        fingerprint="deploy_rejected",
    )
