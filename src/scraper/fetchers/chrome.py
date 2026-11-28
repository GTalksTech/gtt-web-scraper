"""The Chrome Fetcher: real Chrome, for pages the plain download rejects, most often
JavaScript apps that are an empty shell until their scripts run.

It sits between the plain download and the Paid Fetcher on the Ladder. Its Engine is
Google Chrome (the ARM64 build on the Pi) driven by Patchright, a Playwright fork that
hides the automation from the page. Chrome runs in a visible window, on the image's
virtual display (Xvfb), with a persistent profile and the timezone and locale of the
home connection, so it presents as a normal browser (ADR 0001). It reaches sites over
the home connection, so it is spaced per site and respects cool-downs like the plain
download.

Chrome is started on the first page that needs it, then kept running. At most
`max_pages` pages are open at once; more requests wait their turn. After
`restart_after_pages` pages, or a crash, Chrome is closed (once its open pages are
done) and started again on the next page. Chrome that can't start (not installed, no
display) makes the Fetcher unavailable for that request, and the request climbs on to
the Paid Fetcher.

The Engine is behind a small interface (start, render, stop), so tests drive the
Chrome Fetcher with a fake one and never need Chrome.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from collections.abc import Coroutine
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from ..config import ChromeConfig
from ..page import FetchedPage, build_page
from . import ENGINE_UNAVAILABLE, FetchError, FetcherUnavailable

T = TypeVar("T")


@dataclass(frozen=True)
class Rendered:
    """A page as Chrome shows it once its scripts have run."""

    final_url: str
    status: int  # of the last document the tab navigated to
    headers: dict[str, str]  # lower-cased names
    html: str  # the rendered DOM, serialized


class EngineUnavailable(Exception):
    """The Engine couldn't start (Chrome missing, no display)."""


class EngineCrashed(Exception):
    """The browser died while rendering; it must be started again."""


class _NoAnswer(FetchError):
    """Patchright didn't answer in time."""


class Engine(Protocol):
    def start(self) -> None:
        """Start the browser. Raises EngineUnavailable."""

    def render(self, url: str) -> Rendered:
        """Load `url` in a new tab and return it rendered. Safe to call from several
        threads at once. Raises FetchError for a page that couldn't be loaded, and
        EngineCrashed when the browser itself is gone."""
        ...

    def stop(self) -> None:
        """Close the browser. Never raises."""


class ChromeFetcher:
    name = "chrome"
    # A challenge Chrome got may clear when the page is loaded again with the cookie
    # the first visit earned (kept in the profile).
    polls_challenges = True
    home_connection = True

    def __init__(self, config: ChromeConfig) -> None:
        self._config = config
        self._engine: Engine = (config.engine or PatchrightEngine)(config)
        self._cond = threading.Condition()
        self._running = False
        self._crashed = False
        self._restarting = False
        self._open_pages = 0  # pages open right now
        self._pages_since_start = 0
        # Why Chrome last failed to start, and when (time.monotonic()). Until
        # `start_retry_s` has passed, requests are refused at once instead of each
        # waiting out another failed launch.
        self._start_failure: tuple[str, float] | None = None

    def fetch(self, url: str) -> FetchedPage:
        self._open_page()
        try:
            rendered = self._engine.render(url)
        except EngineCrashed as exc:
            with self._cond:
                self._crashed = True
            raise FetchError(f"Chrome crashed: {exc}") from exc
        finally:
            with self._cond:
                self._open_pages -= 1
                self._cond.notify_all()
        return build_page(
            requested_url=url,
            final_url=rendered.final_url,
            status=rendered.status,
            headers=rendered.headers,
            html=rendered.html,
        )

    def _open_page(self) -> None:
        """Wait for a free page, restarting or starting Chrome first if it's due."""
        config = self._config
        with self._cond:
            self._cond.wait_for(
                lambda: self._open_pages < config.max_pages and not self._restarting
            )
            due = self._crashed or self._pages_since_start >= config.restart_after_pages
            if self._running and due:
                self._restarting = True
                self._cond.wait_for(lambda: self._open_pages == 0)
                self._engine.stop()
                self._running = self._crashed = self._restarting = False
                self._cond.notify_all()
            if not self._running:
                self._start()
            self._open_pages += 1
            self._pages_since_start += 1

    def _start(self) -> None:
        """Start Chrome (the lock is held), or refuse while a recent start failed."""
        failure = self._start_failure
        if failure is not None and time.monotonic() - failure[1] < self._config.start_retry_s:
            raise FetcherUnavailable(ENGINE_UNAVAILABLE, failure[0])
        try:
            self._engine.start()
        except EngineUnavailable as exc:
            why = f"Chrome Fetcher unavailable: {exc}"
            self._start_failure = (why, time.monotonic())
            raise FetcherUnavailable(ENGINE_UNAVAILABLE, why) from exc
        self._start_failure = None
        self._running = True
        self._pages_since_start = 0


# Files Chrome leaves in a profile to claim it. A container that stopped uncleanly
# leaves them behind, naming a host that no longer exists, and Chrome then refuses the
# profile as "in use on another computer". Only this process uses the profile.
_PROFILE_LOCKS = ("SingletonLock", "SingletonSocket", "SingletonCookie")
# Extra time for Patchright's own calls around a page load, and for Chrome to start.
_MARGIN_S = 15.0
_LAUNCH_TIMEOUT_S = 60.0


class PatchrightEngine:
    """Google Chrome driven by Patchright's async API, on an event loop of its own in a
    background thread, so pages from several request threads can be open at once."""

    def __init__(self, config: ChromeConfig) -> None:
        self._config = config
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._playwright: Any = None
        self._context: Any = None
        self._closed = False

    def start(self) -> None:
        try:
            from patchright.async_api import async_playwright
        except ImportError as exc:
            raise EngineUnavailable(f"Patchright is not installed ({exc})") from exc
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, name="chrome-engine", daemon=True)
        thread.start()
        self._loop, self._thread = loop, thread
        try:
            self._call(self._launch(async_playwright), _LAUNCH_TIMEOUT_S)
        except Exception as exc:
            self.stop()
            raise EngineUnavailable(_first_line(exc)) from exc

    async def _launch(self, async_playwright: Any) -> None:
        config = self._config
        profile = config.resolved_profile_dir()
        profile.mkdir(parents=True, exist_ok=True)
        for name in _PROFILE_LOCKS:
            (profile / name).unlink(missing_ok=True)
        self._closed = False
        self._playwright = await async_playwright().start()
        # Patchright's advice for passing as a normal browser: real Chrome, a window of
        # the screen's own size, and no custom user agent or headers.
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            channel="chrome",
            headless=config.headless,
            no_viewport=True,
            locale=config.locale,
            timezone_id=config.timezone,
        )
        self._context.on("close", lambda _: setattr(self, "_closed", True))

    def render(self, url: str) -> Rendered:
        if self._loop is None or self._context is None or self._closed:
            raise EngineCrashed("Chrome isn't running")
        config = self._config
        try:
            return self._call(self._render(url), config.timeout_s + config.settle_s + _MARGIN_S)
        except _NoAnswer as exc:
            # Past the page's own load timeout: the browser itself is stuck.
            raise EngineCrashed(str(exc)) from exc

    async def _render(self, url: str) -> Rendered:
        from patchright.async_api import Error as PlaywrightError
        from patchright.async_api import TimeoutError as PlaywrightTimeout

        config = self._config
        try:
            page = await self._context.new_page()
        except PlaywrightError as exc:
            raise EngineCrashed(_first_line(exc)) from exc
        # The tab's last document response: after a challenge clears or a script
        # redirects, it is the page now showing, not the first one loaded.
        last: list[Any] = []

        def on_response(response: Any) -> None:
            if response.request.is_navigation_request() and response.frame == page.main_frame:
                last.append(response)

        page.on("response", on_response)
        try:
            try:
                first = await page.goto(url, wait_until="load", timeout=config.timeout_s * 1000)
            except PlaywrightTimeout as exc:
                raise FetchError(f"Chrome timed out after {config.timeout_s:g}s loading the page") from exc
            # A page that keeps fetching (analytics, live updates) never goes idle; what
            # it has rendered by then is what it gets judged on.
            with suppress(PlaywrightTimeout):
                await page.wait_for_load_state("networkidle", timeout=config.settle_s * 1000)
            html = await _content(page, PlaywrightError)
            response = last[-1] if last else first
            if response is None:
                # No document response at all (e.g. a download): nothing to judge.
                raise FetchError("Chrome got no page for the URL")
            return Rendered(
                final_url=page.url,
                status=response.status,
                headers=await response.all_headers(),
                html=html,
            )
        except PlaywrightError as exc:
            if self._closed:
                raise EngineCrashed(_first_line(exc)) from exc
            raise FetchError(f"Chrome couldn't load the page: {_first_line(exc)}") from exc
        finally:
            with suppress(Exception):
                await page.close()

    def stop(self) -> None:
        loop, thread = self._loop, self._thread
        if loop is None:
            return
        with suppress(Exception):
            self._call(self._shutdown(), _MARGIN_S)
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=_MARGIN_S)
        if not loop.is_running():
            loop.close()
        self._loop = self._thread = None
        self._context = self._playwright = None

    async def _shutdown(self) -> None:
        if self._context is not None:
            with suppress(Exception):
                await self._context.close()
        if self._playwright is not None:
            with suppress(Exception):
                await self._playwright.stop()

    def _call(self, coroutine: Coroutine[Any, Any, T], timeout_s: float) -> T:
        """Run `coroutine` on the Engine's loop and wait for it from this thread."""
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            return future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise _NoAnswer(f"Chrome didn't answer within {timeout_s:g}s") from exc


async def _content(page: Any, error: type[Exception]) -> str:
    """The rendered DOM. A script may be navigating the tab right then (a redirect, a
    challenge clearing); then wait for the new page and read that."""
    try:
        return str(await page.content())
    except error:
        await page.wait_for_load_state("load")
        return str(await page.content())


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text.splitlines()[0]
