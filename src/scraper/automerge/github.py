"""The real PullRequestHost: GitHub's REST API with the workflow's token, standard
library only. It reads the pull request's files as data (their contents at the merge
base and at its head); it never checks out or runs anything from the pull request."""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote

from ..incident_issues.github import GitHubError, GitHubIssues
from . import PullRequest
from .policy import Change, watched

MAX_FILE_PAGES = 30  # GitHub lists at most 3,000 files of a pull request, 100 a page


class GitHubPullRequests(GitHubIssues):
    def pulls_for_commit(self, sha: str) -> list[PullRequest]:
        return [_pull(p) for p in self._call("GET", f"/commits/{sha}/pulls")]

    def changes(self, pull: PullRequest) -> list[Change]:
        base = self._call("GET", f"/compare/{pull.base}...{pull.head_sha}")["merge_base_commit"]["sha"]
        changes: list[Change] = []
        for page in range(1, MAX_FILE_PAGES + 1):
            files = self._call("GET", f"/pulls/{pull.number}/files?per_page=100&page={page}")
            for f in files:
                status = f["status"]
                if status == "renamed":
                    # A rename deletes the old path and adds the new one.
                    old = str(f["previous_filename"])
                    changes.append(self._change(old, base, None))
                    changes.append(self._change(str(f["filename"]), None, pull.head_sha))
                    continue
                changes.append(
                    self._change(
                        str(f["filename"]),
                        None if status == "added" else base,
                        None if status == "removed" else pull.head_sha,
                    )
                )
            if len(files) < 100:
                return changes
        raise GitHubError(f"pull request #{pull.number} changes too many files to review")

    def add_pull_label(self, number: int, label: str) -> None:
        self._call("POST", f"/issues/{number}/labels", {"labels": [label]})

    def merge(self, number: int, sha: str) -> None:
        self._call("PUT", f"/pulls/{number}/merge", {"sha": sha, "merge_method": "squash"})

    def start_workflow(self, workflow: str, ref: str) -> None:
        self._call("POST", f"/actions/workflows/{workflow}/dispatches", {"ref": ref})

    def _change(self, path: str, before: str | None, after: str | None) -> Change:
        """The file at commits `before` and `after` (None where it doesn't exist).
        Only the files a rule reads are fetched; the rest just mark that they changed."""
        if not watched(path):
            return Change(path, None if before is None else b"", None if after is None else b"")
        return Change(path, self._file(path, before), self._file(path, after))

    def _file(self, path: str, ref: str | None) -> bytes | None:
        if ref is None:
            return None
        request = urllib.request.Request(
            f"{self.api}/repos/{self.repo}/contents/{quote(path)}?ref={ref}",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github.raw+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "gtt-web-scraper-automerge",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body: bytes = response.read()
                return body
        except urllib.error.HTTPError as exc:
            raise GitHubError(f"GET {path}@{ref}: HTTP {exc.code}") from exc


def _pull(raw: dict[str, Any]) -> PullRequest:
    head_repo = (raw.get("head") or {}).get("repo") or {}
    return PullRequest(
        number=int(raw["number"]),
        author=str((raw.get("user") or {}).get("login") or ""),
        head_repo=str(head_repo.get("full_name") or ""),
        head_sha=str(raw["head"]["sha"]),
        base=str(raw["base"]["ref"]),
        state=str(raw["state"]),
        draft=bool(raw.get("draft")),
        body=str(raw.get("body") or ""),
        url=str(raw["html_url"]),
    )
