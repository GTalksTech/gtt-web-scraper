"""The Router owns a request end to end: serve the cache, try the Site Adapter for the
URL if there is one, climb the Ladder, judge each result, and return either content
or an explicit failure listing everything tried.

Per site, it starts at the Fetcher that last succeeded there, spaces requests over
the home connection, and puts a site that blocks the home connection on a cool-down
during which only Fetchers that don't use it (the Paid Fetcher) are tried.

A Site Adapter goes before the Ladder. Its result passes the Content Gate like any
Fetcher's; if the adapter fails or its result is rejected, the request climbs the
Ladder as if there were no adapter. An adapter's result is cached like a Fetcher's.
An adapter that reaches its site over the home connection (YouTube, Reddit) is spaced
and respects cool-downs like the plain download, and a 403 or 429 from it starts one;
the GitHub adapter calls GitHub's API host, not the page's. Adapters are not recorded
in per-site memory, which orders the Ladder. A forced Fetcher skips them: the Caller
asked for that Fetcher and nothing else. A Site Adapter can be forced too (the Canary
Set does, to check each one); then nothing else is tried, not even the Ladder.

Every attempt a Fetcher on the Ladder or a Site Adapter makes counts towards its
rolling success rate (health/rates.py); one skipped for a cool-down doesn't.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from .adapters import ADAPTER_NAMES, ApiError, SiteAdapter, build_adapters
from .cache import PageCache
from .config import Config
from .fetchers import COOLING_DOWN, Fetcher, FetchError, FetcherUnavailable
from .fetchers.paid import PaidFetcher
from .fetchers.plain import PlainFetcher
from .gate import BLOCKED, CHALLENGE, FETCH_ERROR, INTERACTIVE_CHALLENGE, Verdict, judge
from .health.rates import Subject, SuccessRate, SuccessRates
from .page import FetchedPage
from .sites import SiteMemory, Spacer, site_of


@dataclass(frozen=True)
class Attempt:
    fetcher: str  # a Fetcher's name, or a Site Adapter's
    verdict: Verdict
    status_code: int | None
    # Set when the Fetcher couldn't be used at all, e.g. "Paid Fetcher unavailable:
    # quota exhausted", or was skipped because the site is on a cool-down.
    unavailable: str | None = None


@dataclass(frozen=True)
class Success:
    page: FetchedPage
    fetcher: str
    verdict: Verdict
    # Fetchers tried for this request; empty when it was served from the cache.
    attempts: list[Attempt]
    # When the page was fetched, if it was served from the cache; None for a live fetch.
    cached_at: float | None = None
    # The Site Adapter that produced the content (then also `fetcher`), or None when a
    # Fetcher on the Ladder did.
    site_adapter: str | None = None


@dataclass(frozen=True)
class Failure:
    attempts: list[Attempt]

    @property
    def unavailable(self) -> list[str]:
        """Fetchers the request needed but couldn't use: why the request failed."""
        return [a.unavailable for a in self.attempts if a.unavailable]


class UnknownFetcher(ValueError):
    """A Caller forced a Fetcher that isn't on the Ladder."""


# Responses that mean a site is blocking the home connection.
_BLOCK_STATUSES = (403, 429)
_BLOCK_REASONS = (CHALLENGE, INTERACTIVE_CHALLENGE, BLOCKED)
# Verdict reasons that mean the page doesn't exist, rather than that a Fetcher failed.
_MISSING_PAGE = ("http_404", "http_410")
# The Verdict reason when a forced Site Adapter doesn't handle the URL.
UNHANDLED_URL = "unhandled_url"


def _build_ladder(config: Config) -> list[Fetcher]:
    available: dict[str, Fetcher] = {
        "plain": PlainFetcher(config.plain),
        "paid": PaidFetcher(config.paid),
    }
    return [available[name] for name in config.ladder]


class Router:
    def __init__(self, config: Config, clock: Callable[[], float] = time.time) -> None:
        self._config = config
        self._ladder = _build_ladder(config)
        self._cache = PageCache(config.cache, clock)
        self._sites = SiteMemory(config.cache.resolved_path(), config.sites, clock)
        self._spacer = Spacer(config.sites.min_interval_s)
        self._adapters = build_adapters(config.adapters)
        self._rates = SuccessRates(config.cache.resolved_path(), clock)

    @property
    def fetchers(self) -> list[str]:
        """Names of the Fetchers on the Ladder, in order: what a Caller may force."""
        return [f.name for f in self._ladder]

    @property
    def site_adapters(self) -> list[str]:
        """Names of the enabled Site Adapters, which a Caller may also force."""
        return [a.name for a in self._adapters]

    def fetcher_rates(self) -> list[SuccessRate]:
        """Each Fetcher's rolling success rate over the configured window, Ladder order."""
        return self._rates.rates("fetcher", self.fetchers, self._config.health.window_s)

    def adapter_rates(self) -> list[SuccessRate]:
        """Each enabled Site Adapter's rolling success rate over the configured window."""
        return self._rates.rates("adapter", self.site_adapters, self._config.health.window_s)

    def get(
        self, url: str, max_age_s: float | None = None, forced: str | None = None
    ) -> Success | Failure:
        """Content for `url`: from the cache if a copy is no older than `max_age_s`
        (default from config; 0 forces a live fetch), else from the Ladder. `forced`
        names the only Fetcher or Site Adapter a live fetch may use: a forced Fetcher
        skips any Site Adapter, and a forced Site Adapter has no Ladder to fall back
        to (a cache hit is still served).
        Raises UnknownFetcher if `forced` is neither on the Ladder nor an enabled
        Site Adapter."""
        forceable = self.fetchers + self.site_adapters
        if forced is not None and forced not in forceable:
            raise UnknownFetcher(
                f"unknown Fetcher {forced!r}; expected one of: {', '.join(forceable)}"
            )
        cached = self._cache.get(url, max_age_s)
        if cached is not None:
            return Success(
                page=cached.page,
                fetcher=cached.fetcher,
                verdict=cached.verdict,
                attempts=[],
                cached_at=cached.fetched_at,
                site_adapter=cached.fetcher if cached.fetcher in ADAPTER_NAMES else None,
            )
        site = site_of(url)
        state = self._sites.lookup(site)
        cooldown = state.cooldown
        attempts: list[Attempt] = []
        forced_adapter = next((a for a in self._adapters if a.name == forced), None)
        if forced_adapter is not None and not forced_adapter.handles(url):
            return Failure([_unhandled(forced_adapter.name, url)])
        adapter = forced_adapter if forced is not None else self._adapter_for(url)
        if adapter is not None and adapter.home_connection and cooldown is not None:
            attempts.append(_skipped(adapter.name, cooldown.explain(adapter.name, site)))
        elif adapter is not None:
            attempt, page = self._try_adapter(adapter, url, site)
            attempts.append(attempt)
            self._count("adapter", attempt)
            if adapter.home_connection and _is_block(attempt):
                cooldown = self._sites.blocked(site, attempt.verdict.reason)
            if page is not None and attempt.verdict.decision != "rejected":
                self._cache.put(url, page, adapter.name, attempt.verdict)
                return Success(
                    page, adapter.name, attempt.verdict, attempts, site_adapter=adapter.name
                )
        for fetcher in self._plan(state.remembered, forced):
            if fetcher.home_connection and cooldown is not None:
                attempts.append(_skipped(fetcher.name, cooldown.explain(fetcher.name, site)))
                continue
            attempt, page = self._try(fetcher, url, site)
            attempts.append(attempt)
            self._count("fetcher", attempt)
            if fetcher.home_connection and _is_block(attempt):
                cooldown = self._sites.blocked(site, attempt.verdict.reason)
            if page is not None and attempt.verdict.decision != "rejected":
                self._sites.succeeded(site, fetcher.name)
                self._cache.put(url, page, fetcher.name, attempt.verdict)
                return Success(page, fetcher.name, attempt.verdict, attempts)
        return Failure(attempts)

    def _count(self, subject: Subject, attempt: Attempt) -> None:
        """Count a Fetcher's or Site Adapter's attempt towards its success rate. A page
        the site says doesn't exist (404, 410) isn't counted either way: it got the
        site's real answer, and the Canary Set asks for such pages every day."""
        if attempt.verdict.reason in _MISSING_PAGE:
            return
        if attempt.unavailable:
            self._rates.record(subject, attempt.fetcher, "unavailable", attempt.verdict.reason)
        elif attempt.verdict.decision == "rejected":
            self._rates.record(subject, attempt.fetcher, "failure", attempt.verdict.reason)
        else:
            self._rates.record(subject, attempt.fetcher, "success")

    def _adapter_for(self, url: str) -> SiteAdapter | None:
        return next((a for a in self._adapters if a.handles(url)), None)

    def _try_adapter(
        self, adapter: SiteAdapter, url: str, site: str
    ) -> tuple[Attempt, FetchedPage | None]:
        """A Site Adapter's result, judged by the Content Gate. A refusal from the
        site's API keeps its status code, so a 403 or 429 can start a cool-down."""
        if adapter.home_connection:
            self._spacer.wait_turn(site)
        try:
            page = adapter.fetch(url)
        except ApiError as exc:
            verdict = Verdict("rejected", f"http_{exc.status}", str(exc))
            return Attempt(adapter.name, verdict, exc.status), None
        except FetchError as exc:
            return Attempt(adapter.name, Verdict("rejected", FETCH_ERROR, str(exc)), None), None
        return Attempt(adapter.name, judge(page, self._config.gate), page.status), page

    def _plan(self, remembered: str | None, forced: str | None) -> list[Fetcher]:
        """The Fetchers to try, in order: only the forced one (none for a forced Site
        Adapter), or the Ladder starting
        at the Fetcher that last succeeded on the site, then the rest in Ladder order."""
        if forced is not None:
            return [f for f in self._ladder if f.name == forced]
        first = [f for f in self._ladder if f.name == remembered]
        return first + [f for f in self._ladder if f.name != remembered]

    def _try(
        self, fetcher: Fetcher, url: str, site: str
    ) -> tuple[Attempt, FetchedPage | None]:
        """One Fetcher's result, re-fetched briefly while a non-interactive challenge
        might clear (unless the Fetcher spends quota per fetch). Interactive challenges
        are returned at once."""
        gate = self._config.gate
        try:
            page = self._fetch(fetcher, url, site)
        except FetcherUnavailable as exc:
            verdict = Verdict("rejected", exc.reason, str(exc))
            return Attempt(fetcher.name, verdict, None, unavailable=str(exc)), None
        except FetchError as exc:
            return Attempt(fetcher.name, Verdict("rejected", FETCH_ERROR, str(exc)), None), None
        verdict = judge(page, gate)
        polls = 0
        max_polls = gate.challenge_poll_attempts if fetcher.polls_challenges else 0
        while verdict.reason == CHALLENGE and polls < max_polls:
            polls += 1
            time.sleep(gate.challenge_poll_interval_s)
            try:
                page = self._fetch(fetcher, url, site)
            except FetchError:
                continue  # keep the challenge Verdict as the cause
            verdict = judge(page, gate)
        if verdict.reason == CHALLENGE:
            verdict = replace(verdict, detail=f"{verdict.detail}; still challenged after {polls} polls")
        return Attempt(fetcher.name, verdict, page.status), page

    def _fetch(self, fetcher: Fetcher, url: str, site: str) -> FetchedPage:
        """One fetch, spaced from the last request to the site if it goes over the
        home connection."""
        if fetcher.home_connection:
            self._spacer.wait_turn(site)
        return fetcher.fetch(url)


def _is_block(attempt: Attempt) -> bool:
    """Whether a rejected result means the site is blocking the home connection."""
    if attempt.verdict.decision != "rejected":
        return False
    return attempt.status_code in _BLOCK_STATUSES or attempt.verdict.reason in _BLOCK_REASONS


def _skipped(name: str, why: str) -> Attempt:
    """A Fetcher or Site Adapter not tried because its site is on a cool-down."""
    return Attempt(name, Verdict("rejected", COOLING_DOWN, why), None, unavailable=why)


def _unhandled(name: str, url: str) -> Attempt:
    """A forced Site Adapter asked for a URL it doesn't know how to get."""
    why = f"the {name} Site Adapter doesn't handle {url}"
    return Attempt(name, Verdict("rejected", UNHANDLED_URL, why), None)
