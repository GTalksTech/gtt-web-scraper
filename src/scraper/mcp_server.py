"""MCP tools for Claude Code (ADR 0003), served over Streamable HTTP at /mcp on the same
port as the Firecrawl-compatible API.

Each tool is added by its own `add_*_tool` function, so a new one (search, ticket #12)
is another function called from `create_mcp_server`. The server is stateless and
answers in plain JSON: no session survives between calls, so a redeploy on pi5 never
strands a connected Claude Code session.
"""

# No `from __future__ import annotations`: FastMCP reads the tool's annotations at
# runtime, and the `fetcher` description names the Ladder's Fetchers from a local.

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import anyio
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from .responses import (
    UNKNOWN_FETCHER,
    attempt_json,
    confidence,
    failure_code,
    failure_error,
    iso_utc,
)
from .router import Failure, Router, Success, UnknownFetcher

INSTRUCTIONS = (
    "gtt-web-scraper turns a URL into clean markdown, trying free Fetchers before the "
    "paid one. It never returns a block page, challenge, cookie wall or empty shell as "
    "content: when no Fetcher gets real content, the tool fails and says why."
)


def create_mcp_server(router: Router) -> FastMCP:
    server = FastMCP(
        "gtt-web-scraper",
        instructions=INSTRUCTIONS,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        # Callers reach the service by its tailnet name (http://pi5:3002/mcp). The
        # tailnet is the access control (spec #1: no app-level auth in v1), so the
        # SDK's localhost-only Host check is off.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    add_fetch_tool(server, router)
    return server


def serve_mcp(app: FastAPI, server: FastMCP) -> None:
    """Answer MCP requests at /mcp on `app`, running the server's session manager for
    as long as the app runs."""
    app.router.routes.extend(server.streamable_http_app().routes)
    outer = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(the_app: Any) -> AsyncIterator[Any]:
        async with server.session_manager.run():
            async with outer(the_app) as state:
                yield state

    app.router.lifespan_context = lifespan


def add_fetch_tool(server: FastMCP, router: Router) -> None:
    fetchers = ", ".join(router.fetchers)

    @server.tool(
        title="Fetch a web page",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    async def fetch(
        url: Annotated[str, Field(description="The page (or PDF) to fetch.")],
        max_age_seconds: Annotated[
            float | None,
            Field(
                ge=0,
                description=(
                    "Oldest cached copy to accept, in seconds. 0 forces a live fetch. "
                    "Omit for the default (24 hours)."
                ),
            ),
        ] = None,
        fetcher: Annotated[
            str | None,
            Field(
                description=(
                    f"Force a live fetch to use only this Fetcher, one of: {fetchers}. "
                    "Omit to climb the Ladder, cheapest first. A cached copy is still served."
                ),
            ),
        ] = None,
    ) -> CallToolResult:
        """Fetch a web page or PDF as clean markdown, with its title, the Fetcher that got
        it, the Content Gate's Verdict and the confidence (accepted, or possibly_partial
        when the content may be incomplete). Fails explicitly, listing every Fetcher
        tried, rather than returning a block page."""
        try:
            outcome = await anyio.to_thread.run_sync(
                lambda: router.get(url, max_age_seconds, fetcher)
            )
        except UnknownFetcher as exc:
            return _error(str(exc), {"code": UNKNOWN_FETCHER, "error": str(exc)})
        if isinstance(outcome, Failure):
            return _fetch_failure(url, outcome)
        return _fetch_success(url, outcome)


def _fetch_success(url: str, outcome: Success) -> CallToolResult:
    page = outcome.page
    cached_at = None if outcome.cached_at is None else iso_utc(outcome.cached_at)
    data: dict[str, Any] = {
        "markdown": page.markdown,
        "title": page.title,
        "url": url,
        "finalUrl": page.final_url,
        "statusCode": page.status,
        "fetcher": outcome.fetcher,
        "verdict": outcome.verdict.decision,
        "confidence": confidence(outcome.verdict),
        "publishedDate": page.published,
        "cachedAt": cached_at,
        "attempts": [attempt_json(a) for a in outcome.attempts],
    }
    header = [
        f"Title: {page.title or 'unknown'}",
        f"URL: {page.final_url}",
        f"Fetcher: {outcome.fetcher}",
        f"Verdict: {outcome.verdict.decision}",
        f"Confidence: {data['confidence']}",
        f"Published: {page.published or 'unknown'}",
    ]
    if cached_at is not None:
        header.append(f"Cached at: {cached_at}")
    text = "\n".join(header) + "\n\n---\n\n" + page.markdown
    return CallToolResult(content=[TextContent(type="text", text=text)], structuredContent=data)


def _fetch_failure(url: str, failure: Failure) -> CallToolResult:
    error = failure_error(url, failure)
    lines = [error, ""]
    for a in failure.attempts:
        status = "" if a.status_code is None else f", HTTP {a.status_code}"
        verdict = f"{a.verdict.decision} ({a.verdict.reason}{status})"
        lines.append(f"- {a.fetcher}: {verdict}: {a.verdict.detail}")
    data = {
        "code": failure_code(failure),
        "error": error,
        "attempts": [attempt_json(a) for a in failure.attempts],
    }
    return _error("\n".join(lines), data)


def _error(text: str, data: dict[str, Any]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text)], structuredContent=data, isError=True
    )
