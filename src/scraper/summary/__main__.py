"""python -m scraper.summary [--month YYYY-MM]

Writes a month's section (default: the month before this one) into the pinned summary
issue. pi5's /health/usage answer for that month is read from the file named by
$USAGE_FILE; if it is missing or not JSON, $DETAIL says why. Needs GH_TOKEN and
GITHUB_REPOSITORY."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..incident_issues.github import GitHubIssues
from . import NoSummaryIssue, previous_month, update_summary


def _usage(path: str, month: str) -> dict[str, Any] | None:
    try:
        usage = json.loads(Path(path).read_text()) if path else None
    except (OSError, ValueError):
        return None
    return usage if isinstance(usage, dict) and usage.get("month") == month else None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scraper.summary")
    parser.add_argument("--month", default=previous_month(datetime.now(timezone.utc)))
    args = parser.parse_args(argv)
    usage = _usage(os.environ.get("USAGE_FILE", ""), args.month)
    detail = os.environ.get("DETAIL", "") or "its answer wasn't that month's usage"
    try:
        update_summary(GitHubIssues.from_env(), args.month, usage, detail)
    except NoSummaryIssue as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"summary for {args.month} written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
