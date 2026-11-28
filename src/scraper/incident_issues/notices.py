"""What Garrett sees of an Incident: one GitHub issue per problem, and an email only
when something needs reading.

GitHub emails the assignee for a new issue and for each new comment, never for an edit
(title, body, labels or a comment's text). So the emails are exactly:

  🚨 gtt-web-scraper: <effect>             a new Incident (the issue itself), or one back
                                           within a week of being fixed (a comment)
  🙋 gtt-web-scraper needs you: <effect>   the agent can't or may not go on (a comment,
                                           after the title changes, so it's the subject)
  ✅ fixed: <effect>                        it's closed as fixed (a comment)

plus at most one "seen again" comment per stretch of repeat detections: later repeats
edit that comment while it's still the newest one, so a check failing every 2 hours
doesn't send an email every 2 hours. Everything else (attempt counts, the agent's pull
request) is a silent edit of the issue body.

The issue body starts with the three lines (what's broken, what the agent is doing,
whether Garrett needs to act), then the evidence, then the pipeline's state in an HTML
comment: the fingerprint that repeat detections are matched on, and when the agent ran.

Limits: the agent gets at most MAX_ATTEMPTS runs per Incident and DAILY_RUNS runs a day
across the repo (counted from those states). Hitting either turns the issue into 🙋, as
does a fix that hasn't landed FIX_LANDS_WITHIN after the agent's last run (`sweep`).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Protocol

OWNER = "GTalksTech"
LABEL = "incident"
AGENT_LABEL = "ready-for-agent"
HUMAN_LABEL = "ready-for-human"

MAX_ATTEMPTS = 2
DAILY_RUNS = 5
RETRY_AFTER = timedelta(hours=12)  # a repeat this soon after a run waits for that run's fix
REOPEN_WITHIN = timedelta(days=7)  # a fixed Incident back this soon is the same one
FIX_LANDS_WITHIN = timedelta(days=2)  # an issue still open this long after a run needs Garrett
EVIDENCE_CHARS = 40_000  # GitHub caps an issue body at 65,536 characters
TITLE_CHARS = 250

# Incidents no change to the code can fix: they go straight to Garrett, and the issue
# says what to do.
NEEDS_YOU: dict[str, str] = {
    "pi_unreachable": (
        "Check pi5 is powered on and connected to Tailscale (`tailscale status`), then"
        " `ssh pi5 systemctl status gtt-web-scraper`. This closes itself once the"
        " reachability check gets an answer again."
    ),
    "ci_tailnet_unavailable": (
        "The CI auth key has probably expired (they last 90 days). Generate a new one"
        " (reusable, ephemeral, pre-approved, tag:ci) and save it with"
        " `gh secret set TS_AUTHKEY`. This closes itself once the check gets through."
    ),
}
DEFAULT_TODO = (
    "Read the evidence and the agent's notes below. Fix it, or close this issue if it"
    " isn't worth fixing."
)
NO_CODE_FIX = "it can't be fixed by changing the code"

_MARKER = re.compile(r"\n*<!-- gtt-incident (\{.*?\}) -->\s*$", re.S)


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    state: str  # "open" or "closed"
    labels: list[str]
    closed_at: datetime | None
    updated_at: datetime
    state_reason: str | None = None  # "completed" or "not_planned" once closed
    created_at: datetime | None = None


class IssueTracker(Protocol):
    """The few GitHub issue operations the pipeline needs."""

    def incident_issues(self, state: str, since: datetime | None = None) -> list[Issue]:
        """Issues labelled LABEL in `state` ("open", "closed" or "all"), updated since."""

    def issue(self, number: int) -> Issue: ...

    def create_issue(self, title: str, body: str, labels: list[str], assignee: str) -> Issue: ...

    def update_issue(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        labels: list[str] | None = None,
        state: str | None = None,
    ) -> None: ...

    def add_comment(self, number: int, body: str) -> int:
        """Adds a comment and returns its id."""

    def edit_comment(self, comment_id: int, body: str) -> None: ...

    def last_comment_id(self, number: int) -> int | None: ...


@dataclass(frozen=True)
class Incident:
    kind: str
    effect: str
    fingerprint: str
    evidence: Any

    @classmethod
    def parse(cls, raw: Any) -> Incident:
        if not isinstance(raw, dict):
            raise ValueError("an Incident is a JSON object")
        fields = {}
        for name in ("kind", "effect", "fingerprint"):
            value = raw.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"an Incident needs a {name}")
            fields[name] = " ".join(value.split())
        return cls(evidence=raw.get("evidence", {}), **fields)


@dataclass
class State:
    """The pipeline's memory of one Incident, kept in its issue body."""

    fingerprint: str
    kind: str
    effect: str
    attempts: list[str] = field(default_factory=list)  # when each agent run started (ISO)
    seen: int = 1  # detections, the first included
    needs_you: str | None = None  # why it's waiting on Garrett, once it is
    repeat_comment: int | None = None  # the "seen again" comment later repeats edit
    pr: str | None = None  # the agent's latest pull request
    todo: str | None = None  # what Garrett should do, when the kind's usual advice won't do

    def runs_since(self, when: datetime) -> int:
        return sum(datetime.fromisoformat(a) >= when for a in self.attempts)

    def last_attempt(self) -> datetime | None:
        return datetime.fromisoformat(self.attempts[-1]) if self.attempts else None


@dataclass(frozen=True)
class Filed:
    """What happened to an Incident: its issue, and whether the agent should run now."""

    issue: int
    run_agent: bool = False
    attempt: int = 0  # which attempt the agent run is (1 or 2), when it runs


# ── entry points ────────────────────────────────────────────────────────────


def file_incident(gh: IssueTracker, raw: Any, now: datetime) -> Filed:
    """An Incident arrived. It opens an issue, joins the open one with its fingerprint,
    or reopens one closed as fixed within REOPEN_WITHIN."""
    incident = Incident.parse(raw)
    for issue in gh.incident_issues("open"):
        if (state := _state(issue)) and state.fingerprint == incident.fingerprint:
            return _repeat(gh, issue, state, incident, now)
    recent = [
        (issue, state)
        for issue in gh.incident_issues("closed", since=now - REOPEN_WITHIN)
        if (state := _state(issue))
        and state.fingerprint == incident.fingerprint
        and issue.state_reason == "completed"
        and issue.closed_at is not None
        and issue.closed_at >= now - REOPEN_WITHIN
    ]
    if recent:
        issue, state = max(recent, key=lambda pair: pair[0].number)
        return _reopen(gh, issue, state, incident, now)
    return _open(gh, incident, now)


def after_attempt(gh: IssueTracker, number: int, outcome: Any, now: datetime) -> Filed:
    """The agent's run on issue `number` ended with `outcome` (its structured output:
    {"outcome": "pr_opened" | "needs_you" | "no_fix", "reason": ..., "pr": ...}), or
    None if the run itself failed. Decides whether it gets another attempt now."""
    issue = gh.issue(number)
    state = _state(issue)
    if state is None or issue.state != "open":
        return Filed(number)
    result = outcome.get("outcome") if isinstance(outcome, dict) else None
    reason = _plain(outcome.get("reason")) if isinstance(outcome, dict) else ""
    if result == "pr_opened":
        state.pr = _plain(outcome.get("pr")) or None
        _save(gh, issue, state, _working(state))
        return Filed(number)
    if result == "needs_you":
        _escalate(gh, issue, state, f"the agent says {NO_CODE_FIX}: {reason or 'no reason given'}")
        return Filed(number)
    return _try_agent(gh, issue, state, now, failed=True)


def closed_as_fixed(gh: IssueTracker, number: int) -> None:
    """Issue `number` was closed as fixed: send the ✅ notice (once)."""
    issue = gh.issue(number)
    state = _state(issue)
    if state is None or issue.title.startswith("✅"):
        return
    _notify_fixed(gh, issue, state)


def held_for_review(gh: IssueTracker, number: int, pr: str, weakened: list[str]) -> None:
    """The agent's pull request `pr` for issue `number` passed its tests but weakens the
    checks (`weakened`, one line each), so it won't merge itself: hand it to Garrett,
    once however often the tests run again."""
    issue = gh.issue(number)
    state = _state(issue)
    if state is None or issue.state != "open" or state.needs_you:
        return
    state.pr = pr
    state.todo = (
        f"Review {pr}: merge it if weakening the checks is right, or close it and fix this"
        " another way."
    )
    reasons = "; ".join(_plain(line) for line in weakened)
    _escalate(gh, issue, state, f"its fix weakens the checks, so it won't merge itself ({reasons})")


def reachability(gh: IssueTracker, *, joined: bool, answered: bool, detail: str, now: datetime) -> Filed | None:
    """The result of one reachability check from GitHub: whether the runner joined the
    tailnet and whether the service on pi5 answered its health check. A failure raises
    an Incident; a success closes the Incidents it ends as fixed."""
    evidence = {"detail": detail[-4000:]}
    if not joined:
        return file_incident(
            gh,
            {
                "kind": "ci_tailnet_unavailable",
                "effect": "the reachability check can't join the tailnet, so a dead Pi would go unnoticed",
                "fingerprint": "ci_tailnet_unavailable",
                "evidence": evidence,
            },
            now,
        )
    _resolve(gh, "ci_tailnet_unavailable")
    if not answered:
        return file_incident(
            gh,
            {
                "kind": "pi_unreachable",
                "effect": "pi5 isn't answering over Tailscale, so every fetch and search fails",
                "fingerprint": "pi_unreachable",
                "evidence": evidence,
            },
            now,
        )
    _resolve(gh, "pi_unreachable")
    return None


def sweep(gh: IssueTracker, now: datetime) -> None:
    """Catch what no event will: an agent run that ended in a pull request that never
    merged (the issue would otherwise say "No, you needn't act" for ever), and an issue
    closed as fixed without its ✅ (a close made with the workflow token starts no
    workflow). Run with every reachability check."""
    for issue in gh.incident_issues("open"):
        state = _state(issue)
        last = state.last_attempt() if state else None
        if state and not state.needs_you and last is not None and now - last >= FIX_LANDS_WITHIN:
            days = FIX_LANDS_WITHIN.days
            _escalate(gh, issue, state, f"the agent's fix hasn't landed {days} days after its last attempt")
    for issue in gh.incident_issues("closed", since=now - REOPEN_WITHIN):
        state = _state(issue)
        if state and issue.state_reason == "completed" and not issue.title.startswith("✅"):
            _notify_fixed(gh, issue, state)


# ── the steps ───────────────────────────────────────────────────────────────


def _open(gh: IssueTracker, incident: Incident, now: datetime) -> Filed:
    state = State(incident.fingerprint, incident.kind, incident.effect)
    blocked = _blocked(gh, state, now)
    if blocked:
        state.needs_you = blocked
        issue = gh.create_issue(
            _title("🙋", state), _body(_needs_you(state), incident.evidence, state), [LABEL, HUMAN_LABEL], OWNER
        )
        return Filed(issue.number)
    state.attempts.append(now.isoformat())
    issue = gh.create_issue(
        _title("🚨", state), _body(_working(state), incident.evidence, state), [LABEL, AGENT_LABEL], OWNER
    )
    return Filed(issue.number, run_agent=True, attempt=1)


def _repeat(gh: IssueTracker, issue: Issue, state: State, incident: Incident, now: datetime) -> Filed:
    state.seen += 1
    text = (
        f"Seen again: {state.seen} detections so far, the latest at {now:%Y-%m-%d %H:%M} UTC."
        f" Latest evidence:\n\n{_evidence(incident.evidence)}"
    )
    if state.repeat_comment is not None and gh.last_comment_id(issue.number) == state.repeat_comment:
        gh.edit_comment(state.repeat_comment, text)
    else:
        state.repeat_comment = gh.add_comment(issue.number, text)
    last = state.last_attempt()
    if state.needs_you or (last is not None and now - last < RETRY_AFTER):
        _save(gh, issue, state)
        return Filed(issue.number)
    return _try_agent(gh, issue, state, now, failed=False)


def _reopen(gh: IssueTracker, issue: Issue, state: State, incident: Incident, now: datetime) -> Filed:
    state = replace(
        state,
        effect=incident.effect,
        seen=state.seen + 1,
        needs_you=None,
        repeat_comment=None,
        pr=None,
        todo=None,
    )
    blocked = _blocked(gh, state, now)
    if blocked:
        state.needs_you = blocked
        lines = _needs_you(state)
        emoji, labels, run = "🙋", [LABEL, HUMAN_LABEL], 0
    else:
        state.attempts.append(now.isoformat())
        lines = _working(state)
        emoji, labels, run = "🚨", [LABEL, AGENT_LABEL], len(state.attempts)
    gh.update_issue(
        issue.number,
        title=_title(emoji, state),
        body=_body(lines, incident.evidence, state),
        labels=labels,
        state="open",
    )
    gh.add_comment(issue.number, "It's back after being fixed.\n\n" + "\n".join(lines))
    return Filed(issue.number, run_agent=bool(run), attempt=run)


def _try_agent(gh: IssueTracker, issue: Issue, state: State, now: datetime, *, failed: bool) -> Filed:
    """Start another agent run on the issue if the limits allow, else hand it to Garrett."""
    blocked = _blocked(gh, state, now)
    if blocked:
        if failed and len(state.attempts) >= MAX_ATTEMPTS:
            blocked = f"the agent tried {MAX_ATTEMPTS} times and couldn't fix it"
        _escalate(gh, issue, state, blocked)
        return Filed(issue.number)
    state.attempts.append(now.isoformat())
    state.pr = None
    _save(gh, issue, state, _working(state))
    return Filed(issue.number, run_agent=True, attempt=len(state.attempts))


def _blocked(gh: IssueTracker, state: State, now: datetime) -> str | None:
    """Why the agent may not run on this Incident now, if it may not."""
    if state.kind in NEEDS_YOU:
        return f"{NO_CODE_FIX}, and the fixing agent can't reach pi5 from GitHub"
    if len(state.attempts) >= MAX_ATTEMPTS:
        return f"the agent has had its {MAX_ATTEMPTS} attempts and the problem is still there"
    day = now - timedelta(days=1)
    runs = sum(s.runs_since(day) for i in gh.incident_issues("all", since=day) if (s := _state(i)))
    if runs >= DAILY_RUNS:
        return f"the fixing agent has used its {DAILY_RUNS} runs for today"
    return None


def _escalate(gh: IssueTracker, issue: Issue, state: State, why: str) -> None:
    state.needs_you = why
    lines = _needs_you(state)
    labels = [label for label in issue.labels if label != AGENT_LABEL]
    gh.update_issue(
        issue.number,
        title=_title("🙋", state),
        body=_rewrite(issue.body, lines, state),
        labels=labels + [HUMAN_LABEL] * (HUMAN_LABEL not in labels),
    )
    # The title changes first, so the email's subject says 🙋.
    gh.add_comment(issue.number, "\n".join(lines))


def _resolve(gh: IssueTracker, fingerprint: str) -> None:
    for issue in gh.incident_issues("open"):
        if (state := _state(issue)) and state.fingerprint == fingerprint:
            _notify_fixed(gh, issue, state)
            gh.update_issue(issue.number, state="closed")


def _notify_fixed(gh: IssueTracker, issue: Issue, state: State) -> None:
    lines = [
        f"**What was broken:** {_sentence(state.effect)}",
        "**What the agent is doing:** Nothing more: it's fixed.",
        "**Do you need to act?** No.",
    ]
    gh.update_issue(
        issue.number,
        title=_title("✅", state),
        body=_rewrite(issue.body, lines, state),
        labels=[label for label in issue.labels if label not in (AGENT_LABEL, HUMAN_LABEL)],
    )
    gh.add_comment(issue.number, f"✅ fixed: {state.effect}\n\nNothing for you to do.")


def _save(gh: IssueTracker, issue: Issue, state: State, lines: list[str] | None = None) -> None:
    gh.update_issue(issue.number, body=_rewrite(issue.body, lines, state))


# ── wording ─────────────────────────────────────────────────────────────────


def _title(emoji: str, state: State) -> str:
    prefix = {
        "🚨": "🚨 gtt-web-scraper: ",
        "🙋": "🙋 gtt-web-scraper needs you: ",
        "✅": "✅ gtt-web-scraper fixed: ",
    }[emoji]
    title = prefix + state.effect
    return title if len(title) <= TITLE_CHARS else title[: TITLE_CHARS - 1] + "…"


def _working(state: State) -> list[str]:
    attempt = len(state.attempts)
    if state.pr:
        doing = f"Claude Code opened a fix ({state.pr}) on attempt {attempt} of {MAX_ATTEMPTS}; it merges itself if the tests pass."
    else:
        doing = (
            f"Claude Code is working on a fix (attempt {attempt} of {MAX_ATTEMPTS}). If the tests"
            " pass, its pull request merges itself, and the Pi checks the new version against"
            " the Canary Set before using it."
        )
    return [
        f"**What's broken:** {_sentence(state.effect)}",
        f"**What the agent is doing:** {doing}",
        "**Do you need to act?** No. You'll get a ✅ when it's fixed, or a 🙋 if it needs you.",
    ]


def _needs_you(state: State) -> list[str]:
    return [
        f"**What's broken:** {_sentence(state.effect)}",
        f"**What the agent is doing:** Nothing more: {state.needs_you}.",
        f"**Do you need to act?** Yes. {state.todo or NEEDS_YOU.get(state.kind, DEFAULT_TODO)}",
    ]


def _sentence(text: str) -> str:
    text = text[:1].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else text + "."


def _plain(value: Any) -> str:
    """Text from the agent, made safe to post: one short line that @-mentions no one."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())[:300].rstrip(".")
    return text.replace("@", "@​")


def _evidence(evidence: Any) -> str:
    text = json.dumps(evidence, indent=2, ensure_ascii=False, sort_keys=True)
    if len(text) > EVIDENCE_CHARS:
        text = text[:EVIDENCE_CHARS] + "\n… (cut short; the full evidence is in incidents-sent.jsonl on pi5)"
    # Page samples come from the web: fence them with more backticks than they hold, so
    # nothing in them renders (no links, images or @-mentions).
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"<details><summary>Evidence</summary>\n\n{fence}json\n{text}\n{fence}\n\n</details>"


# ── the body and the state in it ───────────────────────────────────────────


def _body(lines: list[str], evidence: Any, state: State) -> str:
    return "\n".join(lines) + "\n\n" + _evidence(evidence) + _marker(state)


def _rewrite(body: str, lines: list[str] | None, state: State) -> str:
    """The body with new three lines (or the same ones) and the new state."""
    rest = _MARKER.sub("", body)
    if lines is not None:
        rest = "\n".join(lines + rest.split("\n")[3:])
    return rest + _marker(state)


def _marker(state: State) -> str:
    # "-->" inside the JSON would end the comment early.
    return "\n\n<!-- gtt-incident " + json.dumps(asdict(state), ensure_ascii=False).replace(">", "\\u003e") + " -->"


def _state(issue: Issue) -> State | None:
    match = _MARKER.search(issue.body or "")
    if match is None:
        return None
    try:
        return State(**json.loads(match.group(1)))
    except (TypeError, ValueError):
        return None
