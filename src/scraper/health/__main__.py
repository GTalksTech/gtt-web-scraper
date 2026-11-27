"""The health runner. Run inside a service container on the Pi:

  python -m scraper.health canary [--target URL]
      Runs the Canary Set against the service at URL (default: this container's own,
      http://127.0.0.1:3002) and reads its Fetchers' rolling success rates. Daily, and
      on demand (deploy/pi5/gtt-canary.sh).

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

from ..config import CANARY_SET_FILE, HealthConfig, config_path, load_config
from .canary import (
    CanaryEntry,
    CanaryReport,
    HttpService,
    Service,
    ServiceUnreachable,
    load_canary_set,
    run_canary,
)
from .incidents import Incident, incidents_for, judge_deploy
from .rates import FetcherRate

SELF = "http://127.0.0.1:3002"
KEPT_OUT = 3


def health_run(entries: list[CanaryEntry], service: Service, config: HealthConfig) -> list[Incident]:
    """Run the Canary Set against `service`, read its Fetchers' success rates, and
    return the Incidents they show."""
    report = run_canary(entries, service)
    _log_report(report)
    return incidents_for(report, _rates(service), config)


def _rates(service: Service) -> list[FetcherRate]:
    try:
        status, body = service.get("/health/fetchers")
    except ServiceUnreachable as exc:
        _log(f"success rates unread: {exc}")
        return []
    if status != 200:
        _log(f"success rates unread: HTTP {status}")
        return []
    return [FetcherRate.from_json(r) for r in body.get("fetchers") or []]


def write_incidents(incidents: Sequence[Incident]) -> None:
    for incident in incidents:
        print(json.dumps(incident.to_json(), sort_keys=True), flush=True)
        _log(f"Incident {incident.fingerprint}: {incident.effect}")


def _log_report(report: CanaryReport) -> None:
    _log(f"Canary Set against {report.target}: {report.passed} of {len(report.results)} passing")
    for failure in report.failures:
        _log(
            f"  failed {failure.entry.url}: expected {failure.entry.expected},"
            f" got {failure.verdict} ({failure.reason}) {failure.detail}".rstrip()
        )


def _log(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _answers(service: HttpService, wait_s: float) -> bool:
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


def _canary_of(entries: list[CanaryEntry], service: HttpService, wait_s: float) -> CanaryReport | None:
    if not _answers(service, wait_s):
        _log(f"{service.name} isn't answering its health check")
        return None
    report = run_canary(entries, service)
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
    entries = load_canary_set(config_path().parent / CANARY_SET_FILE)
    if args.command == "canary":
        write_incidents(health_run(entries, HttpService(args.target), config.health))
        return 0
    live = _canary_of(entries, HttpService(args.live), args.wait)
    candidate = _canary_of(entries, HttpService(args.candidate), args.wait)
    decision = judge_deploy(live, candidate, args.candidate_image)
    _log(decision.summary)
    write_incidents(decision.incidents)
    return 0 if decision.promote else KEPT_OUT


if __name__ == "__main__":
    sys.exit(main())
