"""Firecrawl-compatible HTTP API (ADR 0003): only the slice of scrape that Callers use.

Responses keep Firecrawl's shape; our own fields (fetcher, verdict, confidence,
publishedDate, attempts) are extras that Firecrawl-speaking Callers ignore. Requests
take one extra of our own: `fetcher`, to force a specific Fetcher.

A failure (no Fetcher produced real content, including a real upstream 404) is an
HTTP 502 with success: false, so a Caller never mistakes it for content.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .config import Config, load_config
from .router import Attempt, Failure, Router, UnknownFetcher


class ScrapeRequest(BaseModel):
    # Callers send more Firecrawl options than we honour; unknown ones are ignored.
    model_config = ConfigDict(extra="ignore")

    url: str
    # Only markdown is produced; other requested formats are ignored, not errors.
    formats: list[Any] = ["markdown"]
    # Firecrawl's maximum cache age, in milliseconds; 0 forces a live fetch. Omitted
    # means the configured default (24 hours).
    maxAge: int | None = Field(default=None, ge=0)  # noqa: N815 (Firecrawl's field name)
    # Our extra: force a live fetch to use only this Fetcher ("plain" or "paid"),
    # skipping the rest of the Ladder. A cached copy is still served.
    fetcher: str | None = None


def _attempt_json(attempt: Attempt) -> dict[str, Any]:
    return {
        "fetcher": attempt.fetcher,
        "verdict": attempt.verdict.decision,
        "reason": attempt.verdict.reason,
        "detail": attempt.verdict.detail,
        "statusCode": attempt.status_code,
    }


def create_app(
    config: Config | None = None, clock: Callable[[], float] = time.time
) -> FastAPI:
    config = config or load_config()
    router = Router(config, clock)
    app = FastAPI(title="gtt-web-scraper")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/scrape")
    @app.post("/v2/scrape")
    def scrape(request: ScrapeRequest) -> JSONResponse:
        max_age_s = None if request.maxAge is None else request.maxAge / 1000
        try:
            outcome = router.get(request.url, max_age_s, request.fetcher)
        except UnknownFetcher as exc:
            return JSONResponse(
                status_code=422,
                content={"success": False, "code": "UNKNOWN_FETCHER", "error": str(exc)},
            )
        if isinstance(outcome, Failure):
            tried = "; ".join(
                f"{a.fetcher}: {a.verdict.decision} ({a.verdict.reason})" for a in outcome.attempts
            )
            error = f"No Fetcher produced real content for {request.url}. Tried {tried}."
            # A Fetcher the request needed but couldn't use ("Paid Fetcher unavailable:
            # quota exhausted") is the headline: it's why the request failed.
            unavailable = outcome.unavailable
            if unavailable:
                error = f"{'. '.join(unavailable)}. {error}"
            return JSONResponse(
                status_code=502,
                content={
                    "success": False,
                    "code": "FETCHER_UNAVAILABLE" if unavailable else "SCRAPE_REJECTED",
                    "error": error,
                    "attempts": [_attempt_json(a) for a in outcome.attempts],
                },
            )
        page = outcome.page
        accepted = outcome.verdict.decision == "accepted"
        # Firecrawl's cache fields: cacheState is "hit" or "miss"; cachedAt only on a hit.
        cache: dict[str, str] = {"cacheState": "miss"}
        if outcome.cached_at is not None:
            cached_at = datetime.fromtimestamp(outcome.cached_at, UTC)
            cache = {"cacheState": "hit", "cachedAt": cached_at.isoformat().replace("+00:00", "Z")}
        return JSONResponse(
            {
                "success": True,
                "data": {
                    "markdown": page.markdown,
                    "metadata": {
                        "title": page.title,
                        "sourceURL": page.requested_url,
                        "url": page.final_url,
                        "statusCode": page.status,
                        **cache,
                    },
                    "fetcher": outcome.fetcher,
                    "verdict": outcome.verdict.decision,
                    "confidence": "accepted" if accepted else "possibly_partial",
                    "publishedDate": page.published,
                    "attempts": [_attempt_json(a) for a in outcome.attempts],
                },
            }
        )

    return app
