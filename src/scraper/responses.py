"""How a Router or Searcher outcome is described to Callers, shared by the
Firecrawl-compatible API and the MCP tools so both say the same thing about the same
result."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from .gate import Verdict
from .router import Attempt, Failure
from .search import ProviderAttempt, SearchFailure, SearchResult

Confidence = Literal["accepted", "possibly_partial"]
# Why a request returned no content.
FailureCode = Literal["UNKNOWN_FETCHER", "FETCHER_UNAVAILABLE", "SCRAPE_REJECTED"]
UNKNOWN_FETCHER: FailureCode = "UNKNOWN_FETCHER"
# Why a search returned no results: every Search Provider failed.
SearchFailureCode = Literal["SEARCH_FAILED"]
SEARCH_FAILED: SearchFailureCode = "SEARCH_FAILED"


def attempt_json(attempt: Attempt) -> dict[str, Any]:
    return {
        "fetcher": attempt.fetcher,
        "verdict": attempt.verdict.decision,
        "reason": attempt.verdict.reason,
        "detail": attempt.verdict.detail,
        "statusCode": attempt.status_code,
    }


def confidence(verdict: Verdict) -> Confidence:
    """What a Caller is told to make of content: accepted, or Possibly partial when the
    best result was only suspect."""
    return "accepted" if verdict.decision == "accepted" else "possibly_partial"


def failure_code(failure: Failure) -> FailureCode:
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


# What Callers see as the Published date when the Provider and the page give none.
PUBLISHED_UNKNOWN = "unknown"


def published_json(published: str | None) -> str:
    """A Published date (YYYY-MM-DD) as every interface shows it, or "unknown"."""
    return published or PUBLISHED_UNKNOWN


def search_result_json(result: SearchResult) -> dict[str, Any]:
    """One search result as both interfaces show it. The API also writes the snippet
    as Firecrawl's `description`."""
    return {
        "title": result.title,
        "url": result.url,
        "snippet": result.snippet,
        "provider": result.provider,
        "publishedDate": published_json(result.published),
    }


def provider_attempt_json(attempt: ProviderAttempt) -> dict[str, str]:
    return {"provider": attempt.provider, "error": attempt.error}


def search_failure_error(query: str, failure: SearchFailure) -> str:
    """One sentence on why a search came back with nothing, naming each Provider's error."""
    tried = "; ".join(f"{a.provider}: {a.error}" for a in failure.attempts)
    return f"Every Search Provider failed for {query!r}. {tried}."


def search_failure_json(query: str, failure: SearchFailure) -> dict[str, Any]:
    """The failure's code, sentence and per-Provider errors, as both interfaces show them."""
    return {
        "code": SEARCH_FAILED,
        "error": search_failure_error(query, failure),
        "attempts": [provider_attempt_json(a) for a in failure.attempts],
    }


def cached_at_iso(cached_at: float | None) -> str | None:
    """When a cached answer was made, as Firecrawl writes times; None for a live one."""
    return None if cached_at is None else iso_utc(cached_at)


def iso_utc(timestamp: float) -> str:
    """A Unix time as Firecrawl writes it: ISO 8601 in UTC with a Z."""
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")
