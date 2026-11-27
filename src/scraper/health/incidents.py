"""Incidents: what the health runner reports when something degraded, with the evidence
an agent needs to fix it without reproducing it from a data-center address.

Each Incident has a kind, the effect a Caller sees, evidence, and a fingerprint. The
fingerprint names the problem, not the occurrence: the same problem found again on
another day (or failing in a different way) gets the same fingerprint, so it joins
the open Incident instead of opening a new one.

  canary_failed:<site>      Canary pages on one Site didn't get their expected Verdict
  canary_widespread         so much of the Canary Set failed that it's one problem
  fetcher_degraded:<name>   a Fetcher's rolling success rate fell below the floor
  quota_exhausted:<name>    a Fetcher is unavailable because its quota ran out
  deploy_rejected           a new version did worse on the Canary Set and was kept out
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..config import HealthConfig
from ..fetchers import QUOTA_EXHAUSTED
from ..sites import site_of
from .canary import ERROR, CanaryReport, CanaryResult
from .rates import FetcherRate


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
    report: CanaryReport | None, rates: Sequence[FetcherRate], config: HealthConfig
) -> list[Incident]:
    """The Incidents a health run found: from its Canary results (if the Canary Set was
    run) and the Fetchers' rolling success rates. None for a healthy run."""
    incidents = _canary_incidents(report, config) if report is not None else []
    for rate in rates:
        incidents += _rate_incidents(rate, config)
    return incidents


def _canary_incidents(report: CanaryReport, config: HealthConfig) -> list[Incident]:
    failures = report.failures
    if not failures:
        return []
    total = len(report.results)
    if len(failures) >= max(2, math.ceil(config.widespread_fraction * total)):
        down = all(f.verdict == ERROR for f in failures)
        what = "the service isn't answering" if down else "most pages can't be fetched"
        return [
            Incident(
                kind="canary_widespread",
                effect=f"{what} ({len(failures)} of {total} Canary pages failing)",
                evidence=_canary_evidence(report, failures),
                fingerprint="canary_widespread",
            )
        ]
    by_site: dict[str, list[CanaryResult]] = {}
    for failure in failures:
        by_site.setdefault(site_of(failure.entry.url), []).append(failure)
    return [
        Incident(
            kind="canary_failed",
            effect=_site_effect(site, site_failures),
            evidence=_canary_evidence(report, site_failures),
            fingerprint=f"canary_failed:{site}",
        )
        for site, site_failures in by_site.items()
    ]


def _site_effect(site: str, failures: list[CanaryResult]) -> str:
    if all(f.entry.expected == "rejected" for f in failures):
        return f"a page that isn't real content from {site} is passed off as content"
    return f"pages from {site} can't be fetched"


def _canary_evidence(report: CanaryReport, failures: list[CanaryResult]) -> dict[str, Any]:
    return {
        "target": report.target,
        "passed": report.passed,
        "total": len(report.results),
        "failures": [f.evidence() for f in failures],
    }


def _rate_incidents(rate: FetcherRate, config: HealthConfig) -> list[Incident]:
    incidents = []
    if rate.last_unavailable == QUOTA_EXHAUSTED:
        incidents.append(
            Incident(
                kind="quota_exhausted",
                effect=f"the {_fetcher_name(rate.fetcher)}'s quota is used up, so pages that need it fail",
                evidence=rate.to_json(),
                fingerprint=f"quota_exhausted:{rate.fetcher}",
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
                kind="fetcher_degraded",
                effect=(
                    f"the {_fetcher_name(rate.fetcher)} is failing ({success_rate:.0%} of"
                    f" {rate.attempts} attempts succeeded)"
                ),
                evidence={
                    "fetcher": rate.fetcher,
                    "attempts": rate.attempts,
                    "successes": rate.successes,
                    "successRate": round(success_rate, 4),
                    "minSuccessRate": config.min_success_rate,
                },
                fingerprint=f"fetcher_degraded:{rate.fetcher}",
            )
        )
    return incidents


def _fetcher_name(fetcher: str) -> str:
    return "Paid Fetcher" if fetcher == "paid" else f"{fetcher} Fetcher"


@dataclass(frozen=True)
class DeployDecision:
    promote: bool
    summary: str  # one line for the deploy log
    incidents: list[Incident] = field(default_factory=list)


def judge_deploy(
    live: CanaryReport | None, candidate: CanaryReport | None, candidate_image: str
) -> DeployDecision:
    """Whether a new version (`candidate_image`) may take traffic: only if it passes at
    least as much of the Canary Set as the live service. A live service that couldn't
    be checked counts as passing nothing; a candidate that couldn't be checked is kept
    out."""
    live_passed = live.passed if live is not None else 0
    live_score = f"{live_passed} of {len(live.results)}" if live is not None else "unchecked"
    if candidate is None:
        return DeployDecision(
            promote=False,
            summary=f"kept out {candidate_image}: it didn't answer the Canary Set",
            incidents=[
                _deploy_rejected(
                    "it didn't answer the Canary Set",
                    {"candidateImage": candidate_image, "livePassed": live_passed},
                )
            ],
        )
    score = f"{candidate.passed} of {len(candidate.results)}"
    if candidate.passed >= live_passed:
        return DeployDecision(
            promote=True, summary=f"promoting {candidate_image}: {score} passing, live {live_score}"
        )
    live_ok = {r.entry.url for r in live.results if r.passed} if live is not None else set()
    regressions = [r.evidence() for r in candidate.failures if r.entry.url in live_ok]
    return DeployDecision(
        promote=False,
        summary=f"kept out {candidate_image}: {score} passing, live {live_score}",
        incidents=[
            _deploy_rejected(
                f"it did worse on the Canary Set ({score} passing, live {live_score})",
                {
                    "candidateImage": candidate_image,
                    "candidatePassed": candidate.passed,
                    "livePassed": live_passed,
                    "total": len(candidate.results),
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
