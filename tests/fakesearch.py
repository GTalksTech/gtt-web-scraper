"""Seam 1: local fake Search Providers that the search chain calls instead of the real ones.

One server stands in for all five, each under its own path prefix, speaking that
Provider's real wire format (checked against each Provider's docs, 2026-11):

- TinyFish: GET /tinyfish/?query=… with X-API-Key; {"results": [{title, url, snippet, date?}]}
- Exa: POST /exa/search with x-api-key; {"results": [{title, url, publishedDate, highlights}]}
- Tavily: POST /tavily/search with Authorization: Bearer; {"results": [{title, url, content, published_date?}]}
- DuckDuckGo: POST /duckduckgo/html/ (form q=…); the HTML results page ddgs parses.
- SearXNG: GET /searxng/search?q=…&format=json, no key; {"results": [{title, url,
  content, publishedDate}], "unresponsive_engines": [[engine, reason], …]}.

A test tells each Provider to answer with results, fail (HTTP 500) or report an
exhausted quota the way that Provider does, and reads back every query it received.
SearXNG has no quota: run_out("searxng") makes every engine behind it block it, which
it reports as no results plus a list of unresponsive engines.
"""

from __future__ import annotations

import html
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Literal
from urllib.parse import parse_qs, urlsplit

PROVIDERS = ("tinyfish", "exa", "tavily", "duckduckgo", "searxng")
KEYS = {"tinyfish": "tf-test-key", "exa": "exa-test-key", "tavily": "tvly-test-key"}

# How each Provider says its free allowance or credits are used up.
QUOTA_STATUS = {"tinyfish": 429, "exa": 402, "tavily": 432, "duckduckgo": 202}

# What SearXNG says when every engine it asks blocks it: [engine, reason] pairs, sorted
# by engine, as searx/webutils.py get_translated_errors writes them.
SEARXNG_BLOCKED = [
    ["bing", "Suspended: too many requests"],
    ["brave", "timeout"],
    ["google", "Suspended: CAPTCHA"],
]

Mode = Literal["results", "fail", "quota"]


@dataclass(frozen=True)
class Hit:
    title: str
    url: str
    snippet: str
    date: str | None = None


def hits(provider: str, n: int = 3) -> list[Hit]:
    """n distinct results, named for the Provider so a test can tell who answered."""
    return [
        Hit(
            title=f"{provider} result {i}",
            url=f"https://example.org/{provider}/{i}",
            snippet=f"What {provider} says about result {i}.",
        )
        for i in range(1, n + 1)
    ]


class FakeSearchProviders:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._modes: dict[str, Mode] = {p: "results" for p in PROVIDERS}
        self._hits: dict[str, list[Hit]] = {p: hits(p) for p in PROVIDERS}
        # Every query each Provider received, in order.
        self.queries: dict[str, list[str]] = {p: [] for p in PROVIDERS}
        # Every request body (or query string) each Provider received.
        self.requests: dict[str, list[dict[str, object]]] = {p: [] for p in PROVIDERS}
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    def base_url(self, provider: str) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}/{provider}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def answer(self, provider: str, results: list[Hit]) -> None:
        with self._lock:
            self._modes[provider] = "results"
            self._hits[provider] = results

    def fail(self, provider: str) -> None:
        with self._lock:
            self._modes[provider] = "fail"

    def run_out(self, provider: str) -> None:
        with self._lock:
            self._modes[provider] = "quota"

    def calls(self) -> list[str]:
        """Which Providers were asked anything, in the order of their first query."""
        return [p for p in PROVIDERS if self.queries[p]]

    # ── wire formats ──────────────────────────────────────────────────────────

    def _respond(
        self, provider: str, query: str, auth_ok: bool
    ) -> tuple[int, str, bytes]:
        with self._lock:
            self.queries[provider].append(query)
            mode = self._modes[provider]
            results = list(self._hits[provider])
        if not auth_ok:
            return 401, "application/json", b'{"error": "invalid API key"}'
        if mode == "fail":
            return 500, "application/json", b'{"error": "internal error"}'
        if mode == "quota":
            if provider == "searxng":
                return 200, "application/json", _searxng_body(query, [], SEARXNG_BLOCKED)
            if provider == "duckduckgo":
                # DuckDuckGo's rate limit is a 202 with an empty challenge page.
                return 202, "text/html", b"<html><body></body></html>"
            return QUOTA_STATUS[provider], "application/json", json.dumps(
                {"error": "usage limit exceeded"}
            ).encode()
        if provider == "duckduckgo":
            return 200, "text/html", _ddg_page(results).encode()
        body: dict[str, object]
        if provider == "tinyfish":
            body = {
                "query": query,
                "results": [
                    {
                        "position": i,
                        "site_name": "example.org",
                        "title": h.title,
                        "snippet": h.snippet,
                        "url": h.url,
                        **({"date": h.date} if h.date else {}),
                    }
                    for i, h in enumerate(results, 1)
                ],
                "total_results": len(results),
                "page": 0,
            }
        elif provider == "searxng":
            return 200, "application/json", _searxng_body(query, results, [])
        elif provider == "exa":
            body = {
                "requestId": "fake",
                "results": [
                    {
                        "id": h.url,
                        "title": h.title,
                        "url": h.url,
                        "publishedDate": h.date,
                        "author": None,
                        "highlights": [h.snippet],
                    }
                    for h in results
                ],
            }
        else:  # tavily
            body = {
                "query": query,
                "results": [
                    {
                        "title": h.title,
                        "url": h.url,
                        "content": h.snippet,
                        "score": 0.9,
                        **({"published_date": h.date} if h.date else {}),
                    }
                    for h in results
                ],
                "response_time": 0.1,
            }
        return 200, "application/json", json.dumps(body).encode()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (stdlib name)
                parts = urlsplit(self.path)
                params = {k: v[0] for k, v in parse_qs(parts.query).items()}
                if parts.path == "/searxng/search":
                    fake.requests["searxng"].append(dict(params))
                    if params.get("format") != "json":
                        # SearXNG refuses a format its settings don't enable.
                        return self._send(403, "text/html", b"<h1>403 Forbidden</h1>")
                    return self._send(*fake._respond("searxng", params.get("q", ""), True))
                if parts.path.rstrip("/") != "/tinyfish":
                    return self._send(404, "text/plain", b"no such endpoint")
                fake.requests["tinyfish"].append(dict(params))
                auth_ok = self.headers.get("X-API-Key") == KEYS["tinyfish"]
                self._send(*fake._respond("tinyfish", params.get("query", ""), auth_ok))

            def do_POST(self) -> None:  # noqa: N802 (stdlib name)
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                path = urlsplit(self.path).path
                if path == "/exa/search":
                    body = json.loads(raw or b"{}")
                    fake.requests["exa"].append(body)
                    auth_ok = self.headers.get("x-api-key") == KEYS["exa"]
                    return self._send(*fake._respond("exa", body.get("query", ""), auth_ok))
                if path == "/tavily/search":
                    body = json.loads(raw or b"{}")
                    fake.requests["tavily"].append(body)
                    auth_ok = self.headers.get("Authorization") == f"Bearer {KEYS['tavily']}"
                    return self._send(*fake._respond("tavily", body.get("query", ""), auth_ok))
                if path == "/duckduckgo/html/":
                    form = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
                    fake.requests["duckduckgo"].append(dict(form))
                    return self._send(*fake._respond("duckduckgo", form.get("q", ""), True))
                self._send(404, "text/plain", b"no such endpoint")

            def _send(self, status: int, content_type: str, payload: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                pass

        return Handler


def _searxng_body(query: str, results: list[Hit], unresponsive: list[list[str]]) -> bytes:
    """SearXNG's /search?format=json answer (searx/webutils.py get_json_response).
    publishedDate is ISO 8601 without a zone, or null."""
    return json.dumps(
        {
            "query": query,
            "results": [
                {
                    "url": h.url,
                    "title": h.title,
                    "content": h.snippet,
                    "publishedDate": h.date,
                    "engine": "google",
                    "engines": ["google", "brave"],
                    "score": 2.0,
                    "category": "general",
                }
                for h in results
            ],
            "answers": [],
            "corrections": [],
            "infoboxes": [],
            "suggestions": [],
            "unresponsive_engines": unresponsive,
        }
    ).encode()


def _ddg_page(results: list[Hit]) -> str:
    """DuckDuckGo's HTML results page (html.duckduckgo.com/html/), trimmed to the
    structure ddgs reads: one result__body div per result."""
    items = "".join(
        f"""
<div class="result results_links results_links_deep web-result">
  <div class="links_main links_deep result__body">
    <h2 class="result__title">
      <a rel="nofollow" class="result__a" href="{html.escape(h.url)}">{html.escape(h.title)}</a>
    </h2>
    <div class="result__extras"><span class="result__url">{html.escape(h.url)}</span></div>
    <a class="result__snippet" href="{html.escape(h.url)}">{html.escape(h.snippet)}</a>
  </div>
</div>"""
        for h in results
    )
    return f"<html><body><div id=\"links\" class=\"results\">{items}</div></body></html>"
