"""A fake GitHub issue tracker for the Incident pipeline's tests. It keeps issues and
comments in memory and records every email Garrett would get: GitHub emails him for a
new issue assigned to him and for each new comment on it, never for an edit."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

from scraper.pipeline.notices import Issue


@dataclass
class Email:
    subject: str  # the issue's title when it was sent
    text: str


@dataclass
class FakeIssueTracker:
    now: datetime
    issues: dict[int, Issue] = field(default_factory=dict)
    comments: dict[int, list[tuple[int, str]]] = field(default_factory=dict)
    assignees: dict[int, str] = field(default_factory=dict)
    emails: list[Email] = field(default_factory=list)
    _next_id: int = 1000

    def incident_issues(self, state: str, since: datetime | None = None) -> list[Issue]:
        return [
            i
            for i in self.issues.values()
            if "incident" in i.labels
            and (state == "all" or i.state == state)
            and (since is None or i.updated_at >= since)
        ]

    def issue(self, number: int) -> Issue:
        return self.issues[number]

    def create_issue(self, title: str, body: str, labels: list[str], assignee: str) -> Issue:
        number = len(self.issues) + 1
        issue = Issue(number, title, body, "open", list(labels), None, self.now)
        self.issues[number] = issue
        self.comments[number] = []
        self.assignees[number] = assignee
        self.emails.append(Email(title, body))
        return issue

    def update_issue(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        labels: list[str] | None = None,
        state: str | None = None,
    ) -> None:
        issue = self.issues[number]
        closed_at = issue.closed_at
        if state == "closed" and issue.state == "open":
            closed_at = self.now
        if state == "open":
            closed_at = None
        self.issues[number] = replace(
            issue,
            title=title if title is not None else issue.title,
            body=body if body is not None else issue.body,
            labels=list(labels) if labels is not None else issue.labels,
            state=state or issue.state,
            closed_at=closed_at,
            updated_at=self.now,
        )

    def add_comment(self, number: int, body: str) -> int:
        self._next_id += 1
        self.comments[number].append((self._next_id, body))
        self.emails.append(Email(self.issues[number].title, body))
        self.issues[number] = replace(self.issues[number], updated_at=self.now)
        return self._next_id

    def edit_comment(self, comment_id: int, body: str) -> None:
        for number, comments in self.comments.items():
            for at, (cid, _) in enumerate(comments):
                if cid == comment_id:
                    comments[at] = (cid, body)
                    self.issues[number] = replace(self.issues[number], updated_at=self.now)
                    return
        raise KeyError(comment_id)

    def last_comment_id(self, number: int) -> int | None:
        comments = self.comments[number]
        return comments[-1][0] if comments else None

    # ── for the tests ───────────────────────────────────────────────────────

    def comment_texts(self, number: int) -> list[str]:
        return [text for _, text in self.comments[number]]

    def someone_comments(self, number: int, text: str) -> None:
        """A comment by someone else (Garrett, or a cross-reference), not the pipeline."""
        self._next_id += 1
        self.comments[number].append((self._next_id, text))
