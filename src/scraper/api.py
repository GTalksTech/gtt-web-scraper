"""Firecrawl-compatible HTTP API (ADR 0003): only the slice of scrape that Callers use.

Responses keep Firecrawl's shape; our own fields (fetcher, verdict, confidence,
publishedDate, attempts) are extras that Firecrawl-speaking Callers ignore.

A failure (no Fetcher produced real content, including a real upstream 404) is an
HTTP 502 with success: false, so a Caller never mistakes it for content.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from .config import Config, load_config
from .router import Attempt, Failure, Router


class ScrapeRequest(BaseModel):
    # Callers send more Firecrawl options than we honour; unknown ones are ignored.
    model_config = ConfigDict(extra="ignore")

    url: str
    # Only markdown is produced; other requested formats are ignored, not errors.
    formats: list[Any] = ["markdown"]
    maxAge: int | None = None  # noqa: N815 (Firecrawl's field name); accepted, cache comes later


def _attempt_json(attempt: Attempt) -> dict[str, Any]:
    return {
        "fetcher": attempt.fetcher,
        "verdict": attempt.verdict.decision,
        "reason": attempt.verdict.reason,
        "detail": attempt.verdict.detail,
        "statusCode": attempt.status_code,
    }


def create_app(config: Config | None = None) -> FastAPI:
    config = config or load_config()
    router = Router(config)
    app = FastAPI(title="gtt-web-scraper")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/scrape")
    @app.post("/v2/scrape")
    def scrape(request: ScrapeRequest) -> JSONResponse:
        outcome = router.get(request.url)
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
