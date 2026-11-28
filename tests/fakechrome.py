"""Seam 1: a fake Engine for the Chrome Fetcher, so tests never need real Chrome.

It loads pages from the local fake web over HTTP like a browser would, then "runs the
page's JavaScript": every <script type="text/x-fake-js"> block is replaced by the
markup inside it. A fixture page can so be an empty shell to the plain download and
real content to the Chrome Fetcher (tests/fixtures/pages/js_app.html).

It records what the Chrome Fetcher did with it (launches, closes, pages rendered, the
most pages open at once) and can be told to behave like a missing Chrome, a browser
that crashes, or slow pages.
"""

from __future__ import annotations

import re
import threading
import urllib.error
import urllib.request

from scraper.config import ChromeConfig
from scraper.fetchers import FetchError
from scraper.fetchers.chrome import EngineCrashed, EngineUnavailable, Rendered

_FAKE_JS = re.compile(r'<script type="text/x-fake-js">(.*?)</script>', re.S)


class FakeChrome:
    def __init__(self) -> None:
        self.launches = 0
        self.launch_attempts = 0
        self.closes = 0
        self.rendered: list[str] = []
        # The config each Engine was made with, in order.
        self.configs: list[ChromeConfig] = []
        # Chrome isn't installed (or won't start): every launch fails.
        self.missing = False
        # URLs whose render kills the browser.
        self.crash_on: set[str] = set()
        # While set, renders wait until it is released (to hold pages open).
        self.hold: threading.Event | None = None
        self.open_pages = 0
        self.most_open_pages = 0
        self._lock = threading.Lock()

    def engine(self, config: ChromeConfig) -> FakeEngine:
        """The Engine factory the Chrome Fetcher is given (config.chrome.engine)."""
        self.configs.append(config)
        return FakeEngine(self)


class FakeEngine:
    def __init__(self, chrome: FakeChrome) -> None:
        self._chrome = chrome
        self._running = False

    def start(self) -> None:
        self._chrome.launch_attempts += 1
        if self._chrome.missing:
            raise EngineUnavailable("Chrome is not installed")
        self._running = True
        self._chrome.launches += 1

    def stop(self) -> None:
        if self._running:
            self._running = False
            self._chrome.closes += 1

    def render(self, url: str) -> Rendered:
        chrome = self._chrome
        assert self._running, "render() on an Engine that isn't running"
        with chrome._lock:
            chrome.open_pages += 1
            chrome.most_open_pages = max(chrome.most_open_pages, chrome.open_pages)
        try:
            if chrome.hold is not None:
                chrome.hold.wait(timeout=10)
            if url in chrome.crash_on:
                self._running = False
                raise EngineCrashed("the browser closed unexpectedly")
            rendered = _load(url)
            chrome.rendered.append(url)
            return rendered
        finally:
            with chrome._lock:
                chrome.open_pages -= 1


def _load(url: str) -> Rendered:
    request = urllib.request.Request(url, headers={"User-Agent": "FakeChrome"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status, final_url = response.status, response.geturl()
            headers = {k.lower(): v for k, v in response.headers.items()}
            body = response.read()
    except urllib.error.HTTPError as exc:
        status, final_url = exc.code, url
        headers = {k.lower(): v for k, v in exc.headers.items()}
        body = exc.read()
    except OSError as exc:
        raise FetchError(f"net::ERR_CONNECTION_REFUSED ({exc})") from exc
    html = _FAKE_JS.sub(lambda m: m.group(1), body.decode("utf-8", "replace"))
    return Rendered(final_url=final_url, status=status, headers=headers, html=html)
