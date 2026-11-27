"""Firecrawl-compatible HTTP API (ADR 0003): only the slice of scrape that Callers use.

Responses keep Firecrawl's shape; our own fields (fetcher, verdict, confidence,
publishedDate, attempts) are extras that Firecrawl-speaking Callers ignore. Requests
take one extra of our own: `fetcher`, to force a specific Fetcher.

A failure (no Fetcher produced real content, including a real upstream 404) is an
HTTP 502 with success: false, so a Caller never mistakes it for content.

The MCP tools for Claude Code (mcp_server.py) are served by the same app, at /mcp.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .config import Config, load_config
from .mcp_server import create_mcp_server, serve_mcp
from .responses import (
    UNKNOWN_FETCHER,
    attempt_json,
    confidence,
    failure_code,
    failure_error,
    iso_utc,
)
from .router import Failure, Router, UnknownFetcher


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


def create_app(
    config: Config | None = None, clock: Callable[[], float] = time.time
) -> FastAPI:
    config = config or load_config()
    router = Router(config, clock)
    app = FastAPI(title="gtt-web-scraper")
    # The MCP tools for Claude Code, at /mcp on the same port (ADR 0003).
    serve_mcp(app, create_mcp_server(router))

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
                content={"success": False, "code": UNKNOWN_FETCHER, "error": str(exc)},
            )
        if isinstance(outcome, Failure):
            return JSONResponse(
                status_code=502,
                content={
                    "success": False,
                    "code": failure_code(outcome),
                    "error": failure_error(request.url, outcome),
                    "attempts": [attempt_json(a) for a in outcome.attempts],
                },
            )
        page = outcome.page
        # Firecrawl's cache fields: cacheState is "hit" or "miss"; cachedAt only on a hit.
        cache: dict[str, str] = {"cacheState": "miss"}
        if outcome.cached_at is not None:
            cache = {"cacheState": "hit", "cachedAt": iso_utc(outcome.cached_at)}
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
                    "confidence": confidence(outcome.verdict),
                    "publishedDate": page.published,
                    "attempts": [attempt_json(a) for a in outcome.attempts],
                },
            }
        )

    return app
