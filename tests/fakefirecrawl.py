"""Seam 1: a local fake Firecrawl that the Paid Fetcher calls instead of the real one.

It answers Firecrawl's scrape endpoint with whatever a test told it to for a URL,
reports an exhausted quota the way Firecrawl does (HTTP 402) once told to, and
records every scrape request so tests can see what was spent.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .fakeweb import PAGES

API_KEY = "fc-test-key"


@dataclass
class FirecrawlPage:
    """What Firecrawl hands back for one URL: the page as it saw it."""

    markdown: str
    raw_html: str
    status: int = 200
    title: str | None = None
    extra_metadata: dict[str, Any] = field(default_factory=dict)


def firecrawl_page(name: str, markdown: str, status: int = 200) -> FirecrawlPage:
    """A fixture page as Firecrawl would return it, with Firecrawl's own markdown."""
    return FirecrawlPage(markdown=markdown, raw_html=(PAGES / name).read_text(), status=status)


@dataclass
class SeenScrape:
    body: dict[str, Any]
    authorization: str | None


class FakeFirecrawl:
    def __init__(self) -> None:
        self._pages: dict[str, FirecrawlPage] = {}
        self.quota_exhausted = False
        self.scrapes: list[SeenScrape] = []
        # The path of every request Firecrawl received, whatever the endpoint.
        self.paths: list[str] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def serve(self, url: str, result: FirecrawlPage) -> None:
        with self._lock:
            self._pages[url] = result

    def _answer(self, body: dict[str, Any], authorization: str | None) -> tuple[int, dict[str, Any]]:
        if authorization != f"Bearer {API_KEY}":
            return 401, {"success": False, "error": "Unauthorized: Invalid token"}
        if self.quota_exhausted:
            # docs.firecrawl.dev/api-reference/errors: plan credits exhausted, not retryable.
            return 402, {"success": False, "error": "Payment Required: Insufficient credits"}
        url = body.get("url", "")
        with self._lock:
            result = self._pages.get(url)
        if result is None:
            return 500, {"success": False, "error": f"fake Firecrawl has no page for {url}"}
        return 200, {
            "success": True,
            "data": {
                "markdown": result.markdown,
                "rawHtml": result.raw_html,
                "metadata": {
                    "title": result.title,
                    "sourceURL": url,
                    "url": url,
                    "statusCode": result.status,
                    **result.extra_metadata,
                },
            },
        }

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (stdlib name)
                fake.paths.append(self.path)
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802 (stdlib name)
                fake.paths.append(self.path)
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                authorization = self.headers.get("Authorization")
                if self.path == "/v2/scrape":
                    fake.scrapes.append(SeenScrape(body, authorization))
                    status, answer = fake._answer(body, authorization)
                else:
                    status, answer = 404, {"success": False, "error": "no such endpoint"}
                payload = json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                pass

        return Handler
