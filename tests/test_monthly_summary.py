"""The monthly summary (spec #1 story 44): a pinned issue whose body is edited once a
month with the month's requests, the share each Fetcher served, the Paid Fetcher's use
and the Incidents, without sending Garrett an email. Asserted on the issue and the
emails, through the fake tracker."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from scraper.incident_issues.notices import file_incident
from scraper.summary import LABEL, NoSummaryIssue, previous_month, update_summary

from .fakegithub import FakeIssueTracker
from .test_incident_issues import canary_incident

NOV = datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc)
DEC_1 = datetime(2026, 12, 1, 7, 0, tzinfo=timezone.utc)


def usage(month: str = "2026-11", **extra: Any) -> dict[str, Any]:
    return {
        "month": month,
        "pages": {
            "requests": 200,
            "servedBy": {"cache": 90, "plain": 80, "paid": 20, "github": 6, "failed": 4},
        },
        "searches": {"requests": 50, "servedBy": {"tinyfish": 30, "cache": 20}},
        "checks": {"pages": 900, "searches": 120},
        "fetchers": [
            {"fetcher": "plain", "attempts": 120, "successes": 80, "unavailable": 0},
            {"fetcher": "paid", "attempts": 24, "successes": 20, "unavailable": 3},
        ],
        **extra,
    }


def tracker_with_summary_issue() -> tuple[FakeIssueTracker, int]:
    gh = FakeIssueTracker(now=NOV)
    # Garrett opens and pins it once (the setup wizard): GitHub never emails you
    # about your own issue, so it isn't counted as an email here.
    issue = gh.create_issue("📊 gtt-web-scraper monthly summary", "Nothing yet.", [LABEL], "GTalksTech")
    gh.emails.clear()
    return gh, issue.number


def test_the_summary_issue_gets_the_months_numbers_without_an_email() -> None:
    gh, number = tracker_with_summary_issue()
    gh.now = DEC_1

    update_summary(gh, "2026-11", usage(), "")

    body = gh.issue(number).body
    assert "## November 2026" in body
    assert "200 page requests and 50 searches" in body
    assert "| Cache | 90 | 45% |" in body
    assert "| Plain download | 80 | 40% |" in body
    assert "| Paid Fetcher | 20 | 10% |" in body
    assert "| `github` | 6 | 3% |" in body
    assert "| Failed | 4 | 2% |" in body
    assert "24 Firecrawl calls (20 gave content)" in body
    assert "3 requests found its quota exhausted" in body
    assert "900 pages and 120 searches" in body  # the Canary Set's checks, apart
    assert gh.emails == []
    assert gh.comment_texts(number) == []
    assert gh.issue(number).state == "open"


def test_the_incidents_opened_that_month_are_listed_unlinked() -> None:
    gh, number = tracker_with_summary_issue()
    lwn = file_incident(gh, canary_incident("lwn.net"), NOV).issue
    gh.now = DEC_1
    later = file_incident(gh, canary_incident("example.org"), DEC_1).issue
    gh.emails.clear()

    update_summary(gh, "2026-11", usage(), "")

    body = gh.issue(number).body
    assert f"`#{lwn}` 🚨 gtt-web-scraper: pages from lwn.net can't be fetched" in body
    assert "example.org" not in body and f"`#{later}`" not in body
    assert "label%3Aincident+created%3A2026-11-01..2026-11-30" in body
    assert gh.emails == []


def test_a_month_without_incidents_says_so() -> None:
    gh, number = tracker_with_summary_issue()

    update_summary(gh, "2026-11", usage(), "")

    assert "**Incidents:** none." in gh.issue(number).body


def test_when_pi5_does_not_answer_the_summary_still_lists_the_incidents() -> None:
    gh, number = tracker_with_summary_issue()
    file_incident(gh, canary_incident("lwn.net"), NOV)

    update_summary(gh, "2026-11", None, "curl: (28) timed out")

    body = gh.issue(number).body
    assert "pi5 didn't answer" in body and "timed out" in body
    assert "lwn.net" in body


def test_each_month_goes_on_top_and_a_rerun_replaces_its_own_month() -> None:
    gh, number = tracker_with_summary_issue()
    update_summary(gh, "2026-10", usage("2026-10"), "")
    update_summary(gh, "2026-11", usage(), "")
    update_summary(gh, "2026-11", usage(pages={"requests": 7, "servedBy": {"plain": 7}}), "")

    body = gh.issue(number).body
    assert body.index("## November 2026") < body.index("## October 2026")
    assert body.count("## November 2026") == 1
    assert "7 page requests" in body


def test_a_year_of_months_is_kept() -> None:
    gh, number = tracker_with_summary_issue()
    for m in range(1, 13):
        update_summary(gh, f"2025-{m:02d}", usage(f"2025-{m:02d}"), "")
    update_summary(gh, "2026-01", usage("2026-01"), "")

    body = gh.issue(number).body
    assert "## January 2026" in body and "## February 2025" in body
    assert "## January 2025" not in body


def test_text_from_the_pi_cannot_mention_anyone() -> None:
    gh, number = tracker_with_summary_issue()
    file_incident(gh, {**canary_incident(), "effect": "pages from @someone fail"}, NOV)

    update_summary(gh, "2026-11", usage(), "")

    assert "@someone" not in gh.issue(number).body


def test_without_a_summary_issue_nothing_is_created() -> None:
    gh = FakeIssueTracker(now=DEC_1)

    with pytest.raises(NoSummaryIssue):
        update_summary(gh, "2026-11", usage(), "")

    assert gh.issues == {} and gh.emails == []


@pytest.mark.parametrize(
    ("now", "month"),
    [(DEC_1, "2026-11"), (datetime(2027, 1, 1, tzinfo=timezone.utc), "2026-12")],
)
def test_the_job_on_the_1st_sums_up_the_month_before(now: datetime, month: str) -> None:
    assert previous_month(now) == month
    assert previous_month(now + timedelta(days=5)) == month
