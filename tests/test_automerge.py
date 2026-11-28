"""The auto-merge policy (ADR 0002, ADR 0004): the fixing agent's pull request merges
itself once the tests pass, nothing else does, and one that weakens the checks waits
for Garrett. Asserted on what happens to pull requests and incident issues, through a
fake GitHub; the rules on weakening are asserted on file contents before and after."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scraper.automerge import AGENT, PullRequest, review
from scraper.automerge.policy import Change, weakenings
from scraper.incident_issues.github import GitHubError
from scraper.incident_issues.notices import after_attempt, file_incident

from .fakegithub import FakeIssueTracker
from .test_incident_issues import canary_incident

ROOT = Path(__file__).parent.parent
REPO = "GTalksTech/gtt-web-scraper"
T0 = datetime(2026, 11, 27, 6, 0, tzinfo=timezone.utc)


def text(path: str) -> bytes:
    return (ROOT / path).read_bytes()


def edit(path: str, before: str, after: str) -> Change:
    """`path` as it is in the repo, with `before` replaced by `after`."""
    original = text(path).decode()
    assert before in original, f"{before!r} not in {path}"
    return Change(path, original.encode(), original.replace(before, after, 1).encode())


# ── what weakens the checks ────────────────────────────────────────────────


CANARY = "config/canary.yaml"
SOURCES = "config/source-list.yaml"
CORPUS = "tests/corpus/corpus.yaml"
SETTINGS = "config/scraper.yaml"
GATE = "src/scraper/gate.py"

LWN = "- url: https://lwn.net/\n  expected: accepted\n"
GITHUB_404 = (
    "- url: https://github.com/GTalksTech/definitely-not-a-repo-xyz\n"
    "  expected: rejected\n  reason: http_404\n"
)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (edit(CANARY, LWN, ""), "removes Canary URL `https://lwn.net/`"),
        (
            edit(CANARY, LWN, LWN.replace("accepted", "suspect")),
            "loosens Canary URL `https://lwn.net/` (expected: accepted → suspect)",
        ),
        (
            edit(CANARY, GITHUB_404, GITHUB_404.replace("  reason: http_404\n", "")),
            "loosens Canary URL `https://github.com/GTalksTech/definitely-not-a-repo-xyz`"
            " (reason: http_404 → none)",
        ),
        (
            edit(CANARY, LWN, LWN + "  fetcher: auto\n"),
            "loosens Canary URL `https://lwn.net/` (fetcher: none → auto)",
        ),
        (Change(CANARY, text(CANARY), None), "removes Canary URL `https://lwn.net/`"),
        (edit(SOURCES, "  - ietf.org\n", ""), "removes `ietf.org` from the Source List's trusted domains"),
        (edit(SOURCES, "  - answers.com\n", ""), "removes `answers.com` from the Source List's blocked domains"),
        (
            edit(CORPUS, "  expected: accepted\n", "  expected: suspect\n"),
            "relabels Content Gate corpus entry `wikipedia_raspberry_pi`",
        ),
        (
            Change("tests/corpus/pages/wikipedia_raspberry_pi.html.gz", b"old", None),
            "removes or replaces Content Gate corpus page `wikipedia_raspberry_pi.html.gz`",
        ),
        (
            Change("tests/corpus/pages/wikipedia_raspberry_pi.html.gz", b"old", b"new"),
            "removes or replaces Content Gate corpus page `wikipedia_raspberry_pi.html.gz`",
        ),
        (edit(SETTINGS, "  min_chars: 200", "  min_chars: 150"), "loosens gate.min_chars (200 → 150)"),
        (
            edit(SETTINGS, "  accepted_chars: 1000", "  accepted_chars: 900"),
            "loosens gate.accepted_chars (1000 → 900)",
        ),
        (
            edit(SETTINGS, "  min_success_rate: 0.5", "  min_success_rate: 0.3"),
            "loosens health.min_success_rate (0.5 → 0.3)",
        ),
        (
            edit(SETTINGS, "  min_attempts: 10", "  min_attempts: 50"),
            "loosens health.min_attempts (10 → 50)",
        ),
        (
            edit(GATE, '    _Marker("px-captcha", strong=True, interactive=True),', ""),
            "removes Content Gate marker `px-captcha`",
        ),
        (
            Change(".github/workflows/automerge.yml", b"on: x", b"on: y"),
            "changes `.github/workflows/automerge.yml`, which decides what merges",
        ),
        (
            Change("src/scraper/automerge/policy.py", b"x", b"y"),
            "changes `src/scraper/automerge/policy.py`, which decides what merges",
        ),
    ],
)
def test_a_change_that_weakens_the_checks_says_how(change: Change, reason: str) -> None:
    assert reason in weakenings([change])


@pytest.mark.parametrize(
    "change",
    [
        edit(CANARY, LWN, LWN + "- url: https://example.org/new\n  expected: accepted\n"),
        edit(CANARY, LWN, LWN + "  reason: ok\n"),  # pinning a reason tightens it
        edit(SOURCES, "  - ietf.org\n", "  - ietf.org\n  - example.org\n"),
        edit(SOURCES, "  - answers.com\n", "  - answers.com\n  - spam.example\n"),
        edit(CORPUS, "- name: wikipedia_raspberry_pi", "- name: new_page\n  url: https://x.y/\n  status: 200\n  expected: rejected\n\n- name: wikipedia_raspberry_pi"),
        Change("tests/corpus/pages/new_page.html.gz", None, b"new"),
        edit(SETTINGS, "  min_chars: 200", "  min_chars: 250"),
        edit(SETTINGS, "  min_success_rate: 0.5", "  min_success_rate: 0.6"),
        edit(SETTINGS, "  timeout_s: 20", "  timeout_s: 30"),  # not a threshold
        edit(GATE, '_CHALLENGE_TITLES = ("just a moment",', '_CHALLENGE_TITLES = ("just a moment", "please wait",'),
        edit(GATE, '"""The Content Gate:', '"""The Content Gate, reworded:'),  # a docstring
        Change("src/scraper/fetchers/plain.py", b"x", b"y"),
    ],
)
def test_a_change_that_keeps_or_tightens_the_checks_is_not_held(change: Change) -> None:
    assert weakenings([change]) == []


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (Change("tests/test_health.py", b"x", None), "deletes `tests/test_health.py`, part of the test suite"),
        (Change("deploy/pi5/test-deploy.sh", b"x", None), "deletes `deploy/pi5/test-deploy.sh`, part of the test suite"),
        (Change("tests/conftest.py", b"x", b"y"), "changes `tests/conftest.py`, which sets up every test"),
        (
            edit("pyproject.toml", 'testpaths = ["tests"]', 'testpaths = ["tests/corpus"]'),
            "changes how the tests or typecheck run (`[tool.pytest]` in pyproject.toml)",
        ),
        (
            edit("pyproject.toml", 'files = ["src"]', 'files = ["src/scraper/api.py"]'),
            "changes how the tests or typecheck run (`[tool.mypy]` in pyproject.toml)",
        ),
        (
            edit("src/scraper/config.py", 'CANARY_SET_FILE = "canary.yaml"', 'CANARY_SET_FILE = "other.yaml"'),
            "changes which file holds the Canary Set or Source List (`CANARY_SET_FILE`)",
        ),
    ],
)
def test_a_change_that_weakens_the_test_suite_itself_is_held(change: Change, reason: str) -> None:
    assert reason in weakenings([change])


@pytest.mark.parametrize(
    "change",
    [
        Change("tests/test_health.py", b"x", b"y"),  # editing a test is ordinary work
        Change("tests/test_new.py", None, b"y"),
        edit("pyproject.toml", '"ddgs==9.16.1",', '"ddgs==9.17.0",'),  # a dependency bump
        edit("src/scraper/config.py", 'CANARY_SET_FILE = "canary.yaml"', 'CANARY_SET_FILE = "canary.yaml"  # x'),
    ],
)
def test_ordinary_changes_to_tests_and_config_code_are_not_held(change: Change) -> None:
    assert weakenings([change]) == []


def test_a_canary_search_removed_or_loosened_is_held() -> None:
    original = text(CANARY).decode()
    start = original.index("- query:")
    entry = original[start : original.index("\n- ", start + 1) + 1]
    query = entry.split("\n")[0].removeprefix("- query:").strip().strip('"')

    removed = weakenings([Change(CANARY, original.encode(), original.replace(entry, "").encode())])

    assert any(r.startswith("removes Canary search") and query in r for r in removed)


# ── what merges ────────────────────────────────────────────────────────────


@dataclass
class FakeGitHub(FakeIssueTracker):
    """The fake issue tracker, plus pull requests and workflow runs."""

    pulls: dict[int, PullRequest] = field(default_factory=dict)
    pull_changes: dict[int, list[Change]] = field(default_factory=dict)
    merged: list[tuple[int, str]] = field(default_factory=list)
    started: list[tuple[str, str]] = field(default_factory=list)
    pull_labels: dict[int, list[str]] = field(default_factory=dict)

    def pulls_for_commit(self, sha: str) -> list[PullRequest]:
        return [p for p in self.pulls.values() if p.head_sha == sha]

    def changes(self, pull: PullRequest) -> list[Change]:
        return self.pull_changes.get(pull.number, [])

    def add_pull_label(self, number: int, label: str) -> None:
        self.pull_labels.setdefault(number, []).append(label)

    def merge(self, number: int, sha: str) -> None:
        self.merged.append((number, sha))
        self.pulls[number] = replace(self.pulls[number], state="closed")
        for issue in self.pulls[number].fixes():
            self.update_issue(issue, state="closed")

    def start_workflow(self, workflow: str, ref: str) -> None:
        self.started.append((workflow, ref))


def github() -> FakeGitHub:
    return FakeGitHub(now=T0)


def agent_pull(gh: FakeGitHub, issue: int, *, sha: str = "abc123", **changes: object) -> PullRequest:
    number = 100 + len(gh.pulls)
    pull = PullRequest(
        number=number,
        author=AGENT,
        head_repo=REPO,
        head_sha=sha,
        base="main",
        state="open",
        draft=False,
        body=f"Fixes #{issue}\n\nThe gate missed a challenge page.",
        url=f"https://github.com/{REPO}/pull/{number}",
    )
    pull = replace(pull, **changes)  # type: ignore[arg-type]
    gh.pulls[number] = pull
    return pull


def agent_fixing(gh: FakeGitHub) -> int:
    """An Incident whose agent run opened a pull request."""
    filed = file_incident(gh, canary_incident(), T0)
    after_attempt(gh, filed.issue, {"outcome": "pr_opened", "pr": "pull 100", "reason": "x"}, T0)
    return filed.issue


def test_the_agents_pull_request_merges_itself_once_the_tests_pass_and_ships() -> None:
    gh = github()
    issue = agent_fixing(gh)
    pull = agent_pull(gh, issue)

    review(gh, REPO, pull.head_sha)

    assert gh.merged == [(pull.number, pull.head_sha)]
    assert gh.started == [("image.yml", "main")]  # a merge by the workflow token starts nothing


def test_its_incident_gets_the_fixed_notice_when_the_merge_closes_it() -> None:
    gh = github()
    issue = agent_fixing(gh)

    review(gh, REPO, agent_pull(gh, issue).head_sha)

    assert gh.issue(issue).title.startswith("✅")
    assert gh.emails[-1].text.startswith("✅ fixed:")


@pytest.mark.parametrize(
    "outsider",
    [
        {"author": "someone"},
        {"author": "dependabot[bot]"},
        # The Claude app on someone else's fork opens pull requests as claude[bot] too.
        {"head_repo": "someone/gtt-web-scraper"},
        {"base": "some-branch"},
        {"draft": True},
        {"state": "closed"},
    ],
)
def test_nothing_else_merges_itself(outsider: dict[str, object]) -> None:
    gh = github()
    issue = agent_fixing(gh)
    pull = agent_pull(gh, issue, **outsider)

    review(gh, REPO, pull.head_sha)

    assert gh.merged == [] and gh.started == []
    assert not gh.pull_labels  # and it isn't touched


def test_a_pull_request_whose_head_has_moved_on_is_left_for_the_next_test_run() -> None:
    gh = github()
    issue = agent_fixing(gh)
    agent_pull(gh, issue, sha="newer")

    review(gh, REPO, "older")

    assert gh.merged == []


def test_a_fix_that_weakens_the_checks_waits_for_garrett() -> None:
    gh = github()
    issue = agent_fixing(gh)
    pull = agent_pull(gh, issue)
    gh.pull_changes[pull.number] = [edit(CANARY, LWN, "")]
    emails = len(gh.emails)

    review(gh, REPO, pull.head_sha)

    assert gh.merged == [] and gh.started == []
    assert gh.pull_labels[pull.number] == ["ready-for-human"]
    held = gh.issue(issue)
    assert held.title.startswith("🙋 gtt-web-scraper needs you: ")
    assert "removes Canary URL `https://lwn.net/`" in held.body
    assert pull.url in held.body
    [line] = [l for l in held.body.splitlines() if l.startswith("**Do you need to act?**")]
    assert "merge it" in line and "close it" in line
    assert len(gh.emails) == emails + 1  # the 🙋 comment


def test_a_merge_github_refuses_does_not_stop_the_others() -> None:
    gh = github()
    issue = agent_fixing(gh)
    first = agent_pull(gh, issue)
    second = agent_pull(gh, issue)
    refused = first.number

    def merge(number: int, sha: str) -> None:
        if number == refused:
            raise GitHubError("PUT merge: HTTP 405")
        gh.merged.append((number, sha))

    gh.merge = merge  # type: ignore[method-assign]

    log = review(gh, REPO, first.head_sha)

    assert gh.merged == [(second.number, second.head_sha)]
    assert any("405" in line for line in log)


def test_a_held_fix_tested_again_does_not_email_again() -> None:
    gh = github()
    issue = agent_fixing(gh)
    pull = agent_pull(gh, issue)
    gh.pull_changes[pull.number] = [edit(CANARY, LWN, "")]
    review(gh, REPO, pull.head_sha)
    emails = len(gh.emails)

    review(gh, REPO, pull.head_sha)

    assert len(gh.emails) == emails
    assert gh.merged == []


def test_a_held_incident_that_comes_back_after_being_fixed_starts_afresh() -> None:
    gh = github()
    issue = agent_fixing(gh)
    pull = agent_pull(gh, issue)
    gh.pull_changes[pull.number] = [edit(CANARY, LWN, "")]
    review(gh, REPO, pull.head_sha)
    gh.garrett_closes(issue, "completed")

    gh.now = T0 + timedelta(days=1)
    file_incident(gh, canary_incident(), gh.now)

    lines = gh.issue(issue).body.splitlines()
    assert lines[2].startswith("**Do you need to act?** No.")
