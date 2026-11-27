"""The Incident pipeline's steps, as the workflows run them (GH_TOKEN and
GITHUB_REPOSITORY set; results go to $GITHUB_OUTPUT as issue, run-agent, attempt):

  python -m scraper.incident_issues file
      The Incident in $INCIDENT (JSON) arrived: open, join or reopen its issue.
  python -m scraper.incident_issues attempted --issue N
      The agent's run on issue N ended with $OUTCOME (its structured output, or empty
      if the run failed): decide whether it gets another attempt now.
  python -m scraper.incident_issues fixed --issue N
      Issue N was closed as fixed: send the ✅ notice.
  python -m scraper.incident_issues reachability --joined true|false --answered true|false
      One reachability check's result, with what it saw in $DETAIL.
  python -m scraper.incident_issues sweep
      Hand stale fixes to Garrett and send any ✅ a close didn't trigger.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import datetime, timezone

from .github import GitHubIssues
from .notices import Filed, after_attempt, closed_as_fixed, file_incident, reachability, sweep


def _output(filed: Filed | None) -> None:
    lines = {
        "issue": str(filed.issue) if filed else "",
        "run-agent": "true" if filed and filed.run_agent else "false",
        "attempt": str(filed.attempt) if filed else "0",
    }
    for key, value in lines.items():
        print(f"{key}={value}")
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as out:
            out.writelines(f"{key}={value}\n" for key, value in lines.items())


def _json_env(name: str) -> object:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _flag(value: str) -> bool:
    return value.strip().lower() == "true"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scraper.incident_issues")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("file")
    commands.add_parser("sweep")
    for name in ("attempted", "fixed"):
        commands.add_parser(name).add_argument("--issue", type=int, required=True)
    check = commands.add_parser("reachability")
    check.add_argument("--joined", required=True)
    check.add_argument("--answered", required=True)
    args = parser.parse_args(argv)

    gh = GitHubIssues.from_env()
    now = datetime.now(timezone.utc)
    if args.command == "file":
        try:
            _output(file_incident(gh, _json_env("INCIDENT"), now))
        except ValueError as exc:
            print(f"not an Incident: {exc}", file=sys.stderr)
            return 1
    elif args.command == "attempted":
        _output(after_attempt(gh, args.issue, _json_env("OUTCOME"), now))
    elif args.command == "fixed":
        closed_as_fixed(gh, args.issue)
    elif args.command == "sweep":
        sweep(gh, now)
    else:
        _output(
            reachability(
                gh,
                joined=_flag(args.joined),
                answered=_flag(args.answered),
                detail=os.environ.get("DETAIL", ""),
                now=now,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
