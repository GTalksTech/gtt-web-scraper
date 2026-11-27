"""The Incident pipeline on GitHub's side: what Garrett sees when an Incident arrives
from the Pi (or from the reachability check), when the fixing agent tries, gives up,
or fixes it. Asserted on the issues and the emails he gets, through a fake tracker."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from scraper.incident_issues.notices import (
    after_attempt,
    closed_as_fixed,
    file_incident,
    reachability,
    sweep,
)

from .fakegithub import FakeIssueTracker

T0 = datetime(2026, 11, 27, 6, 0, tzinfo=timezone.utc)


def canary_incident(site: str = "lwn.net", sample: str = "Just a moment...") -> dict[str, Any]:
    return {
        "kind": "canary_failed",
        "effect": f"pages from {site} can't be fetched",
        "fingerprint": f"canary_failed:{site}",
        "evidence": {
            "failures": [
                {"url": f"https://{site}/", "verdict": "rejected", "sample": sample}
            ]
        },
    }


def tracker() -> FakeIssueTracker:
    return FakeIssueTracker(now=T0)


def test_a_new_incident_opens_an_issue_that_emails_garrett_and_starts_the_agent() -> None:
    gh = tracker()

    filed = file_incident(gh, canary_incident(), T0)

    issue = gh.issue(filed.issue)
    assert issue.title == "🚨 gtt-web-scraper: pages from lwn.net can't be fetched"
    assert gh.assignees[filed.issue] == "GTalksTech"
    assert set(issue.labels) == {"incident", "ready-for-agent"}
    lines = issue.body.splitlines()
    assert lines[0].startswith("**What's broken:** Pages from lwn.net can't be fetched")
    assert lines[1].startswith("**What the agent is doing:** ")
    assert "attempt 1 of 2" in lines[1]
    assert lines[2].startswith("**Do you need to act?** No.")
    assert "Just a moment..." in issue.body  # the evidence rides along
    assert [e.subject for e in gh.emails] == [issue.title]
    assert filed.run_agent and filed.attempt == 1


def test_a_repeat_detection_joins_the_open_issue_as_a_comment() -> None:
    gh = tracker()
    first = file_incident(gh, canary_incident(), T0)

    again = file_incident(gh, canary_incident(sample="Access denied"), T0 + timedelta(hours=2))

    assert again.issue == first.issue
    assert len(gh.issues) == 1
    [comment] = gh.comment_texts(first.issue)
    assert "Seen again" in comment and "Access denied" in comment
    assert not again.run_agent  # the first attempt's fix may still be on its way


def test_repeats_in_a_row_edit_one_comment_instead_of_emailing_each_time() -> None:
    gh = tracker()
    first = file_incident(gh, canary_incident(), T0)
    for hours in (2, 4, 6):
        file_incident(gh, canary_incident(sample=f"seen at {hours}h"), T0 + timedelta(hours=hours))

    [comment] = gh.comment_texts(first.issue)
    assert "4 detections" in comment and "seen at 6h" in comment
    assert len(gh.emails) == 2  # the issue, and the first "seen again"

    gh.someone_comments(first.issue, "Fixes it? (a note from Garrett)")
    file_incident(gh, canary_incident(sample="seen at 8h"), T0 + timedelta(hours=8))
    assert "seen at 8h" in gh.comment_texts(first.issue)[-1]
    assert len(gh.emails) == 3


def test_a_different_fingerprint_opens_its_own_issue() -> None:
    gh = tracker()
    a = file_incident(gh, canary_incident("lwn.net"), T0)
    b = file_incident(gh, canary_incident("arstechnica.com"), T0)
    assert a.issue != b.issue and b.run_agent


NO_FIX = {"outcome": "no_fix", "reason": "couldn't reproduce"}


def test_a_failed_attempt_gets_one_more_then_the_issue_needs_garrett() -> None:
    gh = tracker()
    filed = file_incident(gh, canary_incident(), T0)

    second = after_attempt(gh, filed.issue, NO_FIX, T0 + timedelta(minutes=20))
    assert second.run_agent and second.attempt == 2
    assert "attempt 2 of 2" in gh.issue(filed.issue).body
    assert len(gh.emails) == 1  # the retry is silent

    done = after_attempt(gh, filed.issue, None, T0 + timedelta(minutes=40))  # the run crashed
    assert not done.run_agent
    issue = gh.issue(filed.issue)
    assert issue.title == "🙋 gtt-web-scraper needs you: pages from lwn.net can't be fetched"
    assert set(issue.labels) == {"incident", "ready-for-human"}
    assert issue.body.splitlines()[2].startswith("**Do you need to act?** Yes.")
    assert "tried 2 times" in issue.body.splitlines()[1]
    assert [e.subject for e in gh.emails][-1] == issue.title  # the 🙋 email
    assert len(gh.emails) == 2


def test_a_repeat_after_a_fix_that_didnt_hold_starts_the_second_attempt() -> None:
    gh = tracker()
    filed = file_incident(gh, canary_incident(), T0)
    after_attempt(gh, filed.issue, {"outcome": "pr_opened", "pr": "https://github.com/x/pull/7", "reason": ""}, T0)
    assert "https://github.com/x/pull/7" in gh.issue(filed.issue).body

    again = file_incident(gh, canary_incident(), T0 + timedelta(days=1))
    assert again.run_agent and again.attempt == 2

    third = file_incident(gh, canary_incident(), T0 + timedelta(days=2))
    assert not third.run_agent
    assert gh.issue(filed.issue).title.startswith("🙋 gtt-web-scraper needs you: ")


def test_the_agent_can_say_it_needs_garrett() -> None:
    gh = tracker()
    filed = file_incident(gh, canary_incident(), T0)

    after_attempt(gh, filed.issue, {"outcome": "needs_you", "reason": "the site now needs a login @someone"}, T0)

    issue = gh.issue(filed.issue)
    assert issue.title.startswith("🙋 ")
    assert "the site now needs a login" in issue.body
    assert "@someone" not in gh.comment_texts(filed.issue)[-1]  # never pings anyone


def test_about_five_agent_runs_a_day_then_new_incidents_need_garrett() -> None:
    gh = tracker()
    runs = [file_incident(gh, canary_incident(f"site{n}.example"), T0 + timedelta(minutes=n)) for n in range(5)]
    assert all(r.run_agent for r in runs)

    sixth = file_incident(gh, canary_incident("site5.example"), T0 + timedelta(hours=1))
    assert not sixth.run_agent
    issue = gh.issue(sixth.issue)
    assert issue.title.startswith("🙋 gtt-web-scraper needs you: ")
    assert "5 runs for today" in issue.body
    assert len(gh.emails) == 6  # opened straight as 🙋: one email, not 🚨 then 🙋

    later = file_incident(gh, canary_incident("site6.example"), T0 + timedelta(days=1, hours=1))
    assert later.run_agent  # a new day


def close(gh: FakeIssueTracker, number: int) -> None:
    """The agent's pull request merged and closed the issue."""
    gh.update_issue(number, state="closed")


def test_closing_a_fixed_incident_sends_one_fixed_notice() -> None:
    gh = tracker()
    filed = file_incident(gh, canary_incident(), T0)
    close(gh, filed.issue)

    closed_as_fixed(gh, filed.issue)
    closed_as_fixed(gh, filed.issue)  # the event delivered twice

    issue = gh.issue(filed.issue)
    assert issue.title == "✅ gtt-web-scraper fixed: pages from lwn.net can't be fetched"
    assert gh.comment_texts(filed.issue) == [
        "✅ fixed: pages from lwn.net can't be fetched\n\nNothing for you to do."
    ]
    assert [e.subject for e in gh.emails][-1] == issue.title
    assert "ready-for-agent" not in issue.labels


def test_an_incident_back_within_a_week_of_its_fix_reopens_the_issue() -> None:
    gh = tracker()
    filed = file_incident(gh, canary_incident(), T0)
    close(gh, filed.issue)
    closed_as_fixed(gh, filed.issue)

    back = file_incident(gh, canary_incident(), T0 + timedelta(days=2))

    assert back.issue == filed.issue and back.run_agent and back.attempt == 2
    issue = gh.issue(filed.issue)
    assert issue.state == "open"
    assert issue.title.startswith("🚨 gtt-web-scraper: ")
    assert "ready-for-agent" in issue.labels
    assert gh.emails[-1].subject == issue.title

    close(gh, filed.issue)
    closed_as_fixed(gh, filed.issue)
    third = file_incident(gh, canary_incident(), T0 + timedelta(days=3))
    assert not third.run_agent  # two fixes that didn't hold: Garrett's turn
    assert gh.issue(filed.issue).title.startswith("🙋 ")


def test_an_incident_back_after_a_week_is_a_new_issue() -> None:
    gh = tracker()
    filed = file_incident(gh, canary_incident(), T0)
    close(gh, filed.issue)
    gh.now = T0 + timedelta(days=1)
    closed_as_fixed(gh, filed.issue)

    back = file_incident(gh, canary_incident(), T0 + timedelta(days=9))
    assert back.issue != filed.issue and back.attempt == 1


def test_the_pi_unreachable_goes_straight_to_garrett_and_closes_itself_when_back() -> None:
    gh = tracker()
    filed = reachability(gh, joined=True, answered=False, detail="curl: (28) timed out", now=T0)

    assert filed is not None and not filed.run_agent
    issue = gh.issue(filed.issue)
    assert issue.title.startswith("🙋 gtt-web-scraper needs you: pi5 isn't answering")
    assert "ssh pi5" in issue.body
    assert "timed out" in issue.body
    reachability(gh, joined=True, answered=False, detail="again", now=T0 + timedelta(hours=2))
    assert len(gh.issues) == 1

    assert reachability(gh, joined=True, answered=True, detail="", now=T0 + timedelta(hours=4)) is None
    issue = gh.issue(filed.issue)
    assert issue.state == "closed"
    assert issue.title.startswith("✅ ")
    assert gh.comment_texts(filed.issue)[-1].startswith("✅ fixed: pi5 isn't answering")


def test_an_expired_ci_key_is_garretts_to_renew() -> None:
    gh = tracker()
    filed = reachability(gh, joined=False, answered=False, detail="invalid key", now=T0)

    assert filed is not None
    issue = gh.issue(filed.issue)
    assert issue.title.startswith("🙋 gtt-web-scraper needs you: the reachability check can't join")
    assert "gh secret set TS_AUTHKEY" in issue.body
    assert not [i for i in gh.issues.values() if "pi5 isn't answering" in i.title]

    reachability(gh, joined=True, answered=True, detail="", now=T0 + timedelta(hours=2))
    assert gh.issue(filed.issue).state == "closed"


def test_a_healthy_check_sends_nothing() -> None:
    gh = tracker()
    assert reachability(gh, joined=True, answered=True, detail="", now=T0) is None
    assert gh.emails == []


def test_evidence_from_the_web_cant_render_or_mention_anyone() -> None:
    gh = tracker()
    filed = file_incident(gh, canary_incident(sample="```\n@GTalksTech ![x](https://evil.example/x.png)"), T0)
    body = gh.issue(filed.issue).body
    assert "````json" in body  # a longer fence than the sample's own


def test_something_that_isnt_an_incident_is_refused() -> None:
    gh = tracker()
    for raw in ({}, {"kind": "x", "effect": "", "fingerprint": "f"}, ["not", "an", "object"]):
        try:
            file_incident(gh, raw, T0)
        except ValueError:
            continue
        raise AssertionError(f"accepted {raw!r}")
    assert gh.issues == {}


def test_an_incident_garrett_closed_as_not_planned_isnt_reopened() -> None:
    gh = tracker()
    filed = file_incident(gh, canary_incident(), T0)
    gh.garrett_closes(filed.issue, "not_planned")

    back = file_incident(gh, canary_incident(), T0 + timedelta(days=1))
    assert back.issue != filed.issue
    assert gh.issue(filed.issue).state == "closed"


def test_a_fix_that_never_lands_turns_into_needs_you() -> None:
    gh = tracker()
    filed = file_incident(gh, canary_incident(), T0)
    after_attempt(gh, filed.issue, {"outcome": "pr_opened", "pr": "https://github.com/x/pull/7", "reason": ""}, T0)

    sweep(gh, T0 + timedelta(days=1))
    assert gh.issue(filed.issue).title.startswith("🚨 ")  # its checks may still be running

    sweep(gh, T0 + timedelta(days=2, hours=1))
    issue = gh.issue(filed.issue)
    assert issue.title.startswith("🙋 gtt-web-scraper needs you: ")
    assert "hasn't landed" in issue.body
    sweep(gh, T0 + timedelta(days=3))
    assert len(gh.emails) == 2  # the 🚨 and one 🙋


def test_a_fixed_incident_closed_without_its_notice_gets_it_from_the_sweep() -> None:
    # A close made with the workflow token starts no workflow, so no ✅ was sent.
    gh = tracker()
    filed = file_incident(gh, canary_incident(), T0)
    gh.garrett_closes(filed.issue, "completed")

    sweep(gh, T0 + timedelta(hours=2))
    sweep(gh, T0 + timedelta(hours=4))

    assert gh.issue(filed.issue).title.startswith("✅ ")
    assert len([t for t in gh.comment_texts(filed.issue) if t.startswith("✅ fixed")]) == 1


def test_the_sweep_leaves_alone_what_garrett_is_handling() -> None:
    gh = tracker()
    waiting = reachability(gh, joined=True, answered=False, detail="", now=T0)
    dropped = file_incident(gh, canary_incident(), T0)
    gh.garrett_closes(dropped.issue, "not_planned")
    emails = len(gh.emails)

    sweep(gh, T0 + timedelta(days=3))

    assert waiting is not None and gh.issue(waiting.issue).title.startswith("🙋 ")
    assert not gh.issue(dropped.issue).title.startswith("✅ ")
    assert len(gh.emails) == emails
