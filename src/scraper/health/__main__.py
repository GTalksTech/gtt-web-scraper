"""The health runner. Run inside a service container on the Pi:

  python -m scraper.health canary [--target URL]
      Runs the Canary Set (pages, Site Adapters and searches) against the service at
      URL (default: this container's own, http://127.0.0.1:3002) and reads the rolling
      success rates of its Fetchers, Search Providers and Site Adapters. Daily, and on
      demand (deploy/pi5/gtt-canary.sh).

  python -m scraper.health deploy --live URL --candidate URL --candidate-image ID
      Runs the Canary Set against the live service and a candidate version, and exits
      0 if the candidate may take traffic or 3 if it is kept out (deploy/pi5/gtt-up.sh).

Each Incident found is written to stdout as one JSON object per line, for the Incident
pipeline to send on; a readable account of the run goes to stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from dataclasses import replace

from ..config import CANARY_SET_FILE, HealthConfig, config_path, load_config
from .canary import (
    CanaryReport,
    CanarySet,
    HttpService,
    Service,
    ServiceUnreachable,
    load_canary_set,
    run_canary,
)
from .incidents import DeployDecision, Incident, incidents_for, judge_deploy
from .rates import Subject, SuccessRate

SELF = "http://127.0.0.1:3002"
KEPT_OUT = 3

# Where the service lists each subject's rolling success rates, and under which key.
_RATE_ENDPOINTS: dict[Subject, tuple[str, str]] = {
    "fetcher": ("/health/fetchers", "fetchers"),
    "provider": ("/health/providers", "providers"),
    "adapter": ("/health/adapters", "adapters"),
}


def health_run(canary: CanarySet, service: Service, config: HealthConfig) -> list[Incident]:
    """Run the Canary Set against `service`, read its success rates, and return the
    Incidents they show."""
    report = run_canary(canary, service)
    _log_report(report)
    return incidents_for(report, _rates(service), config)


def _rates(service: Service) -> list[SuccessRate]:
    rates: list[SuccessRate] = []
    for subject, (path, key) in _RATE_ENDPOINTS.items():
        try:
            status, body = service.get(path)
        except ServiceUnreachable as exc:
            _log(f"{subject} success rates unread: {exc}")
            continue
        if status != 200:
            _log(f"{subject} success rates unread: HTTP {status}")
            continue
        rates += [SuccessRate.from_json(subject, r) for r in body.get(key) or []]
    return rates


def write_incidents(incidents: Sequence[Incident]) -> None:
    for incident in incidents:
        print(json.dumps(incident.to_json(), sort_keys=True), flush=True)
        _log(f"Incident {incident.fingerprint}: {incident.effect}")


def _log_report(report: CanaryReport) -> None:
    _log(f"Canary Set against {report.target}: {report.passed} of {report.total} passing")
    for failure in report.failures:
        _log(
            f"  failed {failure.entry.url}: expected {failure.entry.expected},"
            f" got {failure.verdict} ({failure.reason}) {failure.detail}".rstrip()
        )
    for search in report.search_failures:
        _log(f"  failed {search.key}: {search.outcome} {search.detail}".rstrip())


def _log(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def deploy_check(
    canary: CanarySet,
    live: Service,
    candidate: Service,
    candidate_image: str,
    wait_s: float,
    recheck_pause_s: float = 30.0,
) -> DeployDecision:
    """Run the Canary Set against the live service and a candidate, and decide whether
    the candidate may take traffic. Checks the candidate failed but the live service
    passed are asked once more after a pause, so one blip on a real site or Search
    Provider (a timeout, a 429 the live run provoked) doesn't keep a good version out."""
    live_report = _canary_of(canary, live, wait_s)
    candidate_report = _canary_of(canary, candidate, wait_s)
    if live_report is not None and candidate_report is not None:
        live_ok = {c.key for c in live_report.checks if c.passed}
        again = CanarySet(
            pages=[r.entry for r in candidate_report.failures if r.key in live_ok],
            # Asked of the same Provider again, not of each in turn.
            searches=[
                replace(s.canary, provider=s.provider)
                for s in candidate_report.search_failures
                if s.key in live_ok
            ],
        )
        if again.pages or again.searches:
            _log(f"checking {len(again.pages) + len(again.searches)} check(s) the candidate failed once more")
            time.sleep(recheck_pause_s)
            retried = run_canary(again, candidate)
            pages = {r.key: r for r in retried.results}
            searches = {s.key: s for s in retried.searches}
            candidate_report = replace(
                candidate_report,
                results=[pages.get(r.key, r) for r in candidate_report.results],
                searches=[searches.get(s.key, s) for s in candidate_report.searches],
            )
            _log_report(candidate_report)
    decision = judge_deploy(live_report, candidate_report, candidate_image)
    _log(decision.summary)
    return decision


def _answers(service: Service, wait_s: float) -> bool:
    """Whether `service` answers its health check within `wait_s` (it may be starting)."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            if service.get("/health")[0] == 200:
                return True
        except ServiceUnreachable:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(2)


def _canary_of(canary: CanarySet, service: Service, wait_s: float) -> CanaryReport | None:
    if not _answers(service, wait_s):
        _log(f"{service.name} isn't answering its health check")
        return None
    report = run_canary(canary, service)
    _log_report(report)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scraper.health")
    commands = parser.add_subparsers(dest="command", required=True)
    canary = commands.add_parser("canary", help="run the Canary Set and check success rates")
    canary.add_argument("--target", default=SELF)
    deploy = commands.add_parser("deploy", help="decide whether a candidate may take traffic")
    deploy.add_argument("--live", required=True)
    deploy.add_argument("--candidate", required=True)
    deploy.add_argument("--candidate-image", required=True)
    deploy.add_argument("--wait", type=float, default=120.0, help="seconds to wait for each to start")
    args = parser.parse_args(argv)

    config = load_config()
    canary_set = load_canary_set(config_path().parent / CANARY_SET_FILE)
    if args.command == "canary":
        write_incidents(health_run(canary_set, HttpService(args.target), config.health))
        return 0
    decision = deploy_check(
        canary_set, HttpService(args.live), HttpService(args.candidate), args.candidate_image, args.wait
    )
    write_incidents(decision.incidents)
    return 0 if decision.promote else KEPT_OUT


if __name__ == "__main__":
    sys.exit(main())
