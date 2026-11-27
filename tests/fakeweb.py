"""Seam 1: a local fake web that the service fetches from instead of the internet.

Each path serves a sequence of canned responses (the last one repeats), and every
request is recorded so tests can see what the site saw.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGES = Path(__file__).parent / "fixtures" / "pages"


@dataclass
class FakeResponse:
    body: bytes
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    content_type: str = "text/html; charset=utf-8"


def page(
    name: str,
    status: int = 200,
    headers: dict[str, str] | None = None,
    content_type: str = "text/html; charset=utf-8",
) -> FakeResponse:
    """A fixture page from tests/fixtures/pages."""
    return FakeResponse((PAGES / name).read_bytes(), status, headers or {}, content_type)


@dataclass
class SeenRequest:
    path: str
    headers: dict[str, str]
    at: float  # time.monotonic() when the request arrived


class FakeWeb:
    def __init__(self) -> None:
        self._routes: dict[str, list[FakeResponse]] = {}
        self._served: dict[str, int] = {}
        self.requests: list[SeenRequest] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def serve(self, path: str, *responses: FakeResponse, host: str = "127.0.0.1") -> str:
        """Serve these responses at `path` in order (the last repeats). Returns the URL,
        on `host`: "127.0.0.1" and "localhost" reach the same server as two sites."""
        with self._lock:
            self._routes[path] = list(responses)
            self._served[path] = 0
        return self.url(path, host)

    def url(self, path: str, host: str = "127.0.0.1") -> str:
        port = self._server.server_address[1]
        return f"http://{host}:{port}{path}"

    def hits(self, path: str) -> int:
        return sum(1 for r in self.requests if r.path == path)

    def arrivals(self, *paths: str) -> list[float]:
        """When each request for these paths arrived (time.monotonic()), in order."""
        return [r.at for r in self.requests if r.path in paths]

    def _next(self, path: str) -> FakeResponse | None:
        with self._lock:
            responses = self._routes.get(path)
            if not responses:
                return None
            index = min(self._served[path], len(responses) - 1)
            self._served[path] += 1
            return responses[index]

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        web = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (stdlib name)
                web.requests.append(
                    SeenRequest(
                        self.path,
                        {k.lower(): v for k, v in self.headers.items()},
                        time.monotonic(),
                    )
                )
                response = web._next(self.path) or FakeResponse(b"<h1>404</h1>", 404)
                self.send_response(response.status)
                self.send_header("Content-Type", response.content_type)
                self.send_header("Content-Length", str(len(response.body)))
                for key, value in response.headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(response.body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        return Handler
