"""The monthly summary (spec #1 story 44), run by .github/workflows/monthly-summary.yml
on the 1st: the month before's requests, the share of pages each Fetcher (or the cache,
or a Site Adapter) served, the Paid Fetcher's use, and the Incidents opened.

It only ever edits the body of the summary issue (labelled LABEL, opened and pinned by
Garrett through the setup wizard), and GitHub never emails anyone about an edit. It
never opens that issue itself or comments on it: either would send an email. Incident
numbers are shown in code spans, unlinked, so the incidents get no cross-reference
either. Standard library only, like the Incident pipeline.

Each month is a section, newest first, and a year of them is kept; running a month
again replaces its section.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import quote_plus

from ..incident_issues.notices import LABEL as INCIDENT_LABEL
from ..incident_issues.notices import Issue

LABEL = "monthly-summary"
KEEP_MONTHS = 12
REPO_URL = "https://github.com/GTalksTech/gtt-web-scraper"

HEADER = (
    "# 📊 gtt-web-scraper: monthly summary\n\n"
    "Updated on the 1st of each month by `.github/workflows/monthly-summary.yml`, which only"
    " edits this text, so it never emails you. Nothing here needs you: anything that does"
    " arrives as a 🙋 issue."
)
# What served a page, as the table names it; anything else is a Site Adapter, shown by name.
SERVED_BY = {"cache": "Cache", "plain": "Plain download", "paid": "Paid Fetcher", "failed": "Failed"}
PAID = "paid"

_SECTION = re.compile(r"<!-- summary-month (\d{4}-\d{2}) -->\n(.*?)(?=<!-- summary-month |\Z)", re.S)


class NoSummaryIssue(LookupError):
    pass


class SummaryTracker(Protocol):
    def labelled(self, label: str, state: str, since: datetime | None = None) -> list[Issue]: ...

    def update_issue(self, number: int, *, body: str | None = None) -> None: ...


def previous_month(now: datetime) -> str:
    """The month before the one `now` is in, as YYYY-MM."""
    year, month = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    return f"{year}-{month:02d}"


def update_summary(gh: SummaryTracker, month: str, usage: dict[str, Any] | None, detail: str) -> None:
    """Writes `month`'s section (YYYY-MM) into the summary issue: `usage` as pi5's
    /health/usage gave it, or None with `detail` saying why it couldn't be read.
    Raises NoSummaryIssue if there is no open summary issue."""
    issues = sorted(gh.labelled(LABEL, "open"), key=lambda i: i.number)
    if not issues:
        raise NoSummaryIssue(
            f"no open issue labelled {LABEL}: run scripts/setup/wizard.sh to open and pin it"
        )
    summary = issues[0]
    start, end = _bounds(month)
    incidents = [
        i
        for i in gh.labelled(INCIDENT_LABEL, "all", since=start)
        if i.created_at is not None and start <= i.created_at < end
    ]
    sections = dict(_SECTION.findall(summary.body))
    sections[month] = _section(start, end, usage, detail, incidents)
    kept = sorted(sections.items(), reverse=True)[:KEEP_MONTHS]
    body = HEADER + "".join(f"\n\n<!-- summary-month {m} -->\n{text.strip()}" for m, text in kept)
    gh.update_issue(summary.number, body=body + "\n")


def _bounds(month: str) -> tuple[datetime, datetime]:
    start = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    end = start.replace(year=start.year + start.month // 12, month=start.month % 12 + 1)
    return start, end


def _section(
    start: datetime,
    end: datetime,
    usage: dict[str, Any] | None,
    detail: str,
    incidents: list[Issue],
) -> str:
    lines = [f"## {start:%B %Y}", ""]
    if usage is None:
        why = _plain(detail) or "no detail"
        lines += [f"**Requests:** unknown: pi5 didn't answer when asked for them ({why}).", ""]
    else:
        lines += _usage(usage)
    lines += _incidents(start, end, incidents)
    return "\n".join(lines)


def _usage(usage: dict[str, Any]) -> list[str]:
    pages = usage.get("pages") or {}
    searches = usage.get("searches") or {}
    checks = usage.get("checks") or {}
    page_total = int(pages.get("requests") or 0)
    lines = [
        f"**Requests from Callers:** {page_total:,} page requests and"
        f" {int(searches.get('requests') or 0):,} searches. The Canary Set's own checks"
        f" ({int(checks.get('pages') or 0):,} pages and {int(checks.get('searches') or 0):,}"
        " searches) aren't counted in them.",
        "",
    ]
    if page_total:
        lines += ["| Pages served by | Requests | Share |", "|---|---:|---:|"]
        for name, n in (pages.get("servedBy") or {}).items():
            label = SERVED_BY.get(name, f"`{_plain(name)}`")
            lines.append(f"| {label} | {int(n):,} | {_share(int(n), page_total)} |")
        lines.append("")
    search_total = int(searches.get("requests") or 0)
    if search_total:
        shares = ", ".join(
            f"{SERVED_BY.get(name, _plain(name))} {_share(int(n), search_total)}"
            for name, n in (searches.get("servedBy") or {}).items()
        )
        lines += [f"**Searches served by:** {shares}.", ""]
    paid: dict[str, Any] = next((f for f in usage.get("fetchers") or [] if f.get("fetcher") == PAID), {})
    lines += [
        f"**Paid Fetcher use:** {int(paid.get('attempts') or 0):,} Firecrawl calls"
        f" ({int(paid.get('successes') or 0):,} gave content);"
        f" {int(paid.get('unavailable') or 0):,} requests found its quota exhausted.",
        "",
    ]
    return lines


def _incidents(start: datetime, end: datetime, incidents: list[Issue]) -> list[str]:
    if not incidents:
        return ["**Incidents:** none."]
    query = f"is:issue label:{INCIDENT_LABEL} created:{start:%Y-%m-%d}..{end - timedelta(days=1):%Y-%m-%d}"
    link = f"{REPO_URL}/issues?q={quote_plus(query)}"
    lines = [f"**Incidents:** {len(incidents)} opened ([see them]({link})):", ""]
    lines += [f"- `#{i.number}` {_plain(i.title)}" for i in sorted(incidents, key=lambda i: i.number)]
    return lines


def _share(n: int, total: int) -> str:
    share = 100 * n / total
    return "<1%" if 0 < share < 0.5 else f"{share:.0f}%"


def _plain(text: str) -> str:
    """Text made safe to post: one line, @-mentioning no one."""
    return " ".join(str(text).split())[:300].replace("@", "@​")
