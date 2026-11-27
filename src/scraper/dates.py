"""Published dates: whatever a Search Provider, Firecrawl or a page's metadata reports,
as one calendar date (YYYY-MM-DD), or None when it can't be read.

The date is the one written, in the writer's own time zone: "2026-09-30T22:15:00+02:00"
is 2026-09-30, as trafilatura reads dates from HTML. Relative dates ("3 days ago", as
some Providers write them) need the time they were reported, and count back from it.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime

# Written dates Providers use besides ISO 8601 and RFC 2822 ("Oct 14, 2026").
_FORMATS = (
    "%b %d, %Y",
    "%B %d, %Y",
    "%b %d %Y",
    "%B %d %Y",
    "%d %b %Y",
    "%d %B %Y",
    "%Y/%m/%d",
)
_RELATIVE = re.compile(
    r"^(?:(\d+)|an?) (min|minute|hr|hour|day|week|month|year)s? ago$", re.IGNORECASE
)
_UNIT_DAYS = {
    "min": 1 / 1440,
    "minute": 1 / 1440,
    "hr": 1 / 24,
    "hour": 1 / 24,
    "day": 1,
    "week": 7,
    "month": 30,
    "year": 365,
}
# "Sept 14" and "Oct. 14" as strptime's %b reads them.
_MONTH_SPELLINGS = re.compile(r"^(sept)\b|^([a-z]{3})\.", re.IGNORECASE)
# A UTC zone name after an ISO timestamp ("2026-10-14 10:00:00 UTC").
_UTC_SUFFIX = re.compile(r" (?:UTC|GMT)$", re.IGNORECASE)


def published_date(value: object, reported_at: float | None = None) -> str | None:
    """`value` as YYYY-MM-DD, or None. `reported_at` (a Unix time) is when the value was
    reported: a relative date counts back from it, and a date more than a day after it
    is a mistake, not a Published date. Without it, a relative date can't be read."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = " ".join(value.split()).rstrip(".")
    day = _absolute(text) or _relative(text, reported_at)
    if day is None or day.year < 1900:
        return None
    if reported_at is not None and day > _day(reported_at) + timedelta(days=1):
        return None
    return day.isoformat()


def _absolute(text: str) -> date | None:
    try:
        return datetime.fromisoformat(_UTC_SUFFIX.sub("", text)).date()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text).date()
    except (TypeError, ValueError, IndexError):
        pass
    written = _MONTH_SPELLINGS.sub(lambda m: (m.group(1) or m.group(2))[:3], text)
    for fmt in _FORMATS:
        try:
            return datetime.strptime(written, fmt).date()
        except ValueError:
            continue
    return None


def _relative(text: str, reported_at: float | None) -> date | None:
    if reported_at is None:
        return None
    lowered = text.lower()
    days: float
    if lowered in ("today", "just now"):
        days = 0
    elif lowered == "yesterday":
        days = 1
    else:
        match = _RELATIVE.match(text)
        if match is None:
            return None
        days = int(match.group(1) or 1) * _UNIT_DAYS[match.group(2).lower()]
    return (datetime.fromtimestamp(reported_at, UTC) - timedelta(days=days)).date()


def _day(timestamp: float) -> date:
    return datetime.fromtimestamp(timestamp, UTC).date()
