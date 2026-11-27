"""Auto-merge (ADR 0002, ADR 0004), run by .github/workflows/automerge.yml each time the
tests pass on a pull request. Only the fixing agent's pull requests merge themselves:
opened by the Claude GitHub app (claude[bot]) from a branch of this repo, so a pull
request from a fork never does, even one the Claude app opened there. One that weakens
the checks (policy.py) waits for Garrett: it is labelled, and its Incident turns 🙋.

The merge is made with the workflow's own token, which starts no workflow, so this
starts the image build itself and sends the Incident's ✅.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from ..incident_issues.github import GitHubError
from ..incident_issues.notices import HUMAN_LABEL, IssueTracker, closed_as_fixed, held_for_review
from .policy import Change, weakenings

AGENT = "claude[bot]"
BASE = "main"
IMAGE_WORKFLOW = "image.yml"
MAX_REASONS = 10

_FIXES = re.compile(r"\b(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)\s+#(\d+)\b", re.I)


@dataclass(frozen=True)
class PullRequest:
    number: int
    author: str  # the login that opened it
    head_repo: str  # owner/name of the repository its branch is in
    head_sha: str
    base: str
    state: str  # "open" or "closed"
    draft: bool
    body: str
    url: str

    def fixes(self) -> list[int]:
        """The issues its body says it fixes ("Fixes #12")."""
        return [int(n) for n in _FIXES.findall(self.body)]


class PullRequestHost(IssueTracker, Protocol):
    """The GitHub operations auto-merge needs, beside the issue tracker's."""

    def pulls_for_commit(self, sha: str) -> list[PullRequest]: ...

    def changes(self, pull: PullRequest) -> list[Change]:
        """The files the pull request changes, with their contents before and after
        for the files the policy reads."""

    def add_pull_label(self, number: int, label: str) -> None: ...

    def merge(self, number: int, sha: str) -> None:
        """Merges it, if its head is still `sha` (and the required checks passed)."""

    def start_workflow(self, workflow: str, ref: str) -> None: ...


def review(gh: PullRequestHost, repo: str, sha: str) -> list[str]:
    """The tests passed on commit `sha` of `repo`: merge each of the agent's open pull
    requests whose head it is, unless it weakens the checks. Returns what was done,
    one line per pull request, for the log."""
    log = []
    for pull in gh.pulls_for_commit(sha):
        why_not = _not_the_agents(pull, repo, sha)
        if why_not:
            log.append(f"#{pull.number}: left alone ({why_not})")
            continue
        weakened = weakenings(gh.changes(pull))
        if weakened:
            _hold(gh, pull, weakened)
            log.append(f"#{pull.number}: held for Garrett ({'; '.join(weakened)})")
            continue
        try:
            gh.merge(pull.number, sha)
        except GitHubError as exc:
            # A conflict, or the required check not passed after all: the next test run
            # (or Garrett) decides; the other pull requests still get theirs.
            log.append(f"#{pull.number}: GitHub refused the merge ({exc})")
            continue
        gh.start_workflow(IMAGE_WORKFLOW, BASE)
        for number in pull.fixes():
            issue = gh.issue(number)
            if issue.state == "closed" and issue.state_reason == "completed":
                closed_as_fixed(gh, number)
        log.append(f"#{pull.number}: merged")
    return log


def _not_the_agents(pull: PullRequest, repo: str, sha: str) -> str | None:
    if pull.author != AGENT:
        return f"opened by {pull.author}, not {AGENT}"
    if pull.head_repo != repo:
        return f"its branch is in {pull.head_repo}, not {repo}"
    if pull.base != BASE:
        return f"it targets {pull.base}, not {BASE}"
    if pull.state != "open" or pull.draft:
        return "it isn't open and ready"
    if pull.head_sha != sha:
        return "its head has moved on; the next test run decides"
    return None


def _hold(gh: PullRequestHost, pull: PullRequest, weakened: list[str]) -> None:
    gh.add_pull_label(pull.number, HUMAN_LABEL)
    shown = weakened[:MAX_REASONS]
    if len(weakened) > MAX_REASONS:
        shown.append(f"and {len(weakened) - MAX_REASONS} more")
    for number in pull.fixes():
        held_for_review(gh, number, pull.url, shown)
