"""How a Router outcome is described to Callers, shared by the scrape API and the MCP
tools so both say the same thing about the same result."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .gate import Verdict
from .router import Attempt, Failure


def attempt_json(attempt: Attempt) -> dict[str, Any]:
    return {
        "fetcher": attempt.fetcher,
        "verdict": attempt.verdict.decision,
        "reason": attempt.verdict.reason,
        "detail": attempt.verdict.detail,
        "statusCode": attempt.status_code,
    }


def confidence(verdict: Verdict) -> str:
    """What a Caller is told to make of content: accepted, or Possibly partial when the
    best result was only suspect."""
    return "accepted" if verdict.decision == "accepted" else "possibly_partial"


def failure_code(failure: Failure) -> str:
    return "FETCHER_UNAVAILABLE" if failure.unavailable else "SCRAPE_REJECTED"


def failure_error(url: str, failure: Failure) -> str:
    """One sentence on why no content came back, naming every Fetcher tried."""
    tried = "; ".join(
        f"{a.fetcher}: {a.verdict.decision} ({a.verdict.reason})" for a in failure.attempts
    )
    error = f"No Fetcher produced real content for {url}. Tried {tried}."
    # A Fetcher the request needed but couldn't use ("Paid Fetcher unavailable: quota
    # exhausted") is the headline: it's why the request failed.
    unavailable = failure.unavailable
    if unavailable:
        error = f"{'. '.join(unavailable)}. {error}"
    return error


def iso_utc(timestamp: float) -> str:
    """A Unix time as Firecrawl writes it: ISO 8601 in UTC with a Z."""
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")
