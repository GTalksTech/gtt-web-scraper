"""Firecrawl-compatible HTTP API (ADR 0003): only the slice of scrape and search that
Callers use.

Responses keep Firecrawl's shape; our own fields (fetcher, siteAdapter, verdict,
confidence, publishedDate as YYYY-MM-DD or "unknown", attempts) are extras that
Firecrawl-speaking Callers ignore. Requests take one extra of our own: on scrape,
`fetcher`, to force a specific Fetcher or Site Adapter; on search, `provider`, to force
a specific Search Provider.

A failure (no Fetcher produced real content, including a real upstream 404) is an
HTTP 502 with success: false, so a Caller never mistakes it for content.

Search takes a query and a limit and answers with title, URL and description per result
(v1: a list in `data`; v2: `data.web`), re-ranked by the Source List, plus our extras:
each result's `provider`, `snippet` and `publishedDate` (YYYY-MM-DD or "unknown", as on
scrape), and the search's `provider`, `cacheState`, `cachedAt` and failed `attempts`. Search
never calls Firecrawl. When every Search Provider fails, it is an HTTP 502 with
success: false naming each Provider's error, never an empty list.

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
    cached_at_iso,
    confidence,
    failure_code,
    failure_error,
    iso_utc,
    provider_attempt_json,
    published_json,
    search_failure_json,
    search_result_json,
)
from .router import Failure, Router, UnknownFetcher
from .search import Searcher, SearchFailure, SearchSuccess, UnknownProvider

# A search forced to a Search Provider that isn't configured.
UNKNOWN_PROVIDER = "UNKNOWN_PROVIDER"


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
    # skipping the rest of the Ladder and any Site Adapter, or only this Site Adapter
    # ("github"…), with no Ladder to fall back to. A cached copy is still served.
    fetcher: str | None = None


# Puts the list of web results where a Firecrawl API version expects them in `data`.
WebShape = Callable[[list[dict[str, Any]]], Any]


class SearchRequest(BaseModel):
    # Firecrawl search options we don't honour (sources, tbs, scrapeOptions…) are ignored.
    model_config = ConfigDict(extra="ignore")

    query: str = Field(min_length=1, max_length=2000)
    # Omitted means Firecrawl's default for the API version (v1: 5, v2: 10).
    limit: int | None = Field(default=None, ge=1, le=100)
    # Our extra: ask only this Search Provider, live (the cache isn't read), and don't
    # pass the query on if it fails. The Canary Set uses it to check each Provider.
    provider: str | None = None


def create_app(
    config: Config | None = None, clock: Callable[[], float] = time.time
) -> FastAPI:
    config = config or load_config()
    router = Router(config, clock)
    searcher = Searcher(config, clock)
    app = FastAPI(title="gtt-web-scraper")
    # The MCP tools for Claude Code, at /mcp on the same port (ADR 0003).
    serve_mcp(app, create_mcp_server(router, searcher))

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # Rolling success rates, for the health runner (our own endpoints; Firecrawl has
    # none like them).
    @app.get("/health/fetchers")
    def fetcher_health() -> dict[str, Any]:
        """Each Fetcher's rolling success rate, in Ladder order."""
        return {
            "windowSeconds": config.health.window_s,
            "fetchers": [rate.to_json() for rate in router.fetcher_rates()],
        }

    @app.get("/health/providers")
    def provider_health() -> dict[str, Any]:
        """Each Search Provider's rolling success rate, in the order a query tries them."""
        return {
            "windowSeconds": config.health.window_s,
            "providers": [rate.to_json() for rate in searcher.provider_rates()],
        }

    @app.get("/health/adapters")
    def adapter_health() -> dict[str, Any]:
        """Each enabled Site Adapter's rolling success rate."""
        return {
            "windowSeconds": config.health.window_s,
            "adapters": [rate.to_json() for rate in router.adapter_rates()],
        }

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
                    # The Site Adapter that produced the content, or null for the Ladder.
                    "siteAdapter": outcome.site_adapter,
                    "verdict": outcome.verdict.decision,
                    "confidence": confidence(outcome.verdict),
                    "publishedDate": published_json(page.published),
                    "attempts": [attempt_json(a) for a in outcome.attempts],
                },
            }
        )

    def search(request: SearchRequest, default_limit: int, shape: WebShape) -> JSONResponse:
        try:
            outcome = searcher.search(
                request.query, request.limit or default_limit, request.provider
            )
        except UnknownProvider as exc:
            return JSONResponse(
                status_code=422,
                content={"success": False, "code": UNKNOWN_PROVIDER, "error": str(exc)},
            )
        if isinstance(outcome, SearchFailure):
            return JSONResponse(
                status_code=502,
                content={"success": False, **search_failure_json(request.query, outcome)},
            )
        return _search_success(outcome, shape)

    @app.post("/v1/search")
    def search_v1(request: SearchRequest) -> JSONResponse:
        # v1: the results are `data` itself.
        return search(request, default_limit=5, shape=lambda web: web)

    @app.post("/v2/search")
    def search_v2(request: SearchRequest) -> JSONResponse:
        # v2: results are grouped by source; we only search the web.
        return search(request, default_limit=10, shape=lambda web: {"web": web})

    return app


def _search_success(outcome: SearchSuccess, shape: WebShape) -> JSONResponse:
    web = [{"description": r.snippet, **search_result_json(r)} for r in outcome.results]
    cached_at = cached_at_iso(outcome.cached_at)
    return JSONResponse(
        {
            "success": True,
            "data": shape(web),
            # Our extras: the Provider that answered, whether the cache did, and the
            # Providers that failed before it.
            "provider": outcome.provider,
            "cacheState": "miss" if cached_at is None else "hit",
            "cachedAt": cached_at,
            "attempts": [provider_attempt_json(a) for a in outcome.attempts],
        }
    )
