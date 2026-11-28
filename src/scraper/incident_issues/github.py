"""The real IssueTracker: GitHub's REST API with the workflow's token, standard library
only. Issues and comments made with the workflow token appear as github-actions[bot],
so GitHub emails the assignee (it never emails you about your own issues)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Self
from urllib.parse import urlencode

from .notices import LABEL, Issue


class GitHubError(RuntimeError):
    pass


class GitHubIssues:
    def __init__(self, repo: str, token: str, api: str = "https://api.github.com") -> None:
        self.repo = repo
        self.token = token
        self.api = api.rstrip("/")

    @classmethod
    def from_env(cls) -> Self:
        return cls(
            os.environ["GITHUB_REPOSITORY"],
            os.environ["GH_TOKEN"],
            os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        )

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        request = urllib.request.Request(
            f"{self.api}/repos/{self.repo}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "gtt-web-scraper-incidents",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise GitHubError(f"{method} {path}: HTTP {exc.code} {exc.read()[:500]!r}") from exc
        return json.loads(raw) if raw else None

    def incident_issues(self, state: str, since: datetime | None = None) -> list[Issue]:
        return self.labelled(LABEL, state, since)

    def labelled(self, label: str, state: str, since: datetime | None = None) -> list[Issue]:
        """Issues (not pull requests) labelled `label` in `state`, updated since."""
        query: dict[str, str] = {"labels": label, "state": state, "per_page": "100"}
        if since is not None:
            query["since"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        issues: list[Issue] = []
        for page in range(1, 11):
            batch = self._call("GET", f"/issues?{urlencode({**query, 'page': str(page)})}")
            issues += [_issue(i) for i in batch if "pull_request" not in i]
            if len(batch) < 100:
                break
        return issues

    def issue(self, number: int) -> Issue:
        return _issue(self._call("GET", f"/issues/{number}"))

    def create_issue(self, title: str, body: str, labels: list[str], assignee: str) -> Issue:
        created = self._call(
            "POST", "/issues", {"title": title, "body": body, "labels": labels, "assignees": [assignee]}
        )
        return _issue(created)

    def update_issue(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        labels: list[str] | None = None,
        state: str | None = None,
    ) -> None:
        change: dict[str, Any] = {}
        if title is not None:
            change["title"] = title
        if body is not None:
            change["body"] = body
        if labels is not None:
            change["labels"] = labels
        if state is not None:
            change["state"] = state
            if state == "closed":
                change["state_reason"] = "completed"
        self._call("PATCH", f"/issues/{number}", change)

    def add_comment(self, number: int, body: str) -> int:
        return int(self._call("POST", f"/issues/{number}/comments", {"body": body})["id"])

    def edit_comment(self, comment_id: int, body: str) -> None:
        self._call("PATCH", f"/issues/comments/{comment_id}", {"body": body})

    def last_comment_id(self, number: int) -> int | None:
        count = int(self._call("GET", f"/issues/{number}")["comments"])
        if not count:
            return None
        page = (count + 99) // 100
        comments = self._call("GET", f"/issues/{number}/comments?per_page=100&page={page}")
        return int(comments[-1]["id"]) if comments else None


def _issue(raw: dict[str, Any]) -> Issue:
    return Issue(
        number=int(raw["number"]),
        title=str(raw["title"]),
        body=str(raw.get("body") or ""),
        state=str(raw["state"]),
        labels=[str(label["name"]) for label in raw.get("labels") or []],
        closed_at=_time(raw.get("closed_at")),
        updated_at=_time(raw.get("updated_at")) or datetime.fromtimestamp(0).astimezone(),
        state_reason=raw.get("state_reason"),
        created_at=_time(raw.get("created_at")),
    )


def _time(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
