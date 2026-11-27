"""python -m scraper.automerge --sha SHA

The tests passed on commit SHA: merge the fixing agent's pull requests whose head it
is, unless they weaken the checks. Needs GH_TOKEN and GITHUB_REPOSITORY."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import review
from .github import GitHubPullRequests


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scraper.automerge")
    parser.add_argument("--sha", required=True)
    args = parser.parse_args(argv)
    gh = GitHubPullRequests.from_env()
    for line in review(gh, gh.repo, args.sha) or ["no open pull request has this head"]:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
