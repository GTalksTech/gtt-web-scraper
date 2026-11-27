"""Firecrawl-compatible HTTP API (ADR 0003): only the slice of scrape that Callers use.

Responses keep Firecrawl's shape; our own fields (fetcher, verdict, confidence,
publishedDate, attempts) are extras that Firecrawl clients ignore.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from .config import Config, load_config
from .router import Attempt, Failure, Router


class ScrapeRequest(BaseModel):
    # Firecrawl clients send more options than we honour; unknown ones are ignored.
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
            return JSONResponse(
                status_code=502,
                content={
                    "success": False,
                    "code": "SCRAPE_REJECTED",
                    "error": f"No Fetcher produced real content for {request.url}. Tried {tried}.",
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
