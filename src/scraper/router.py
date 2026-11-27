"""The Router owns a request end to end: climb the Ladder, judge each result, and
return either content or an explicit failure listing every Fetcher tried.

Per site, it starts at the Fetcher that last succeeded there, spaces requests over
the home connection, and puts a site that blocks the home connection on a cool-down
during which only Fetchers that don't use it (the Paid Fetcher) are tried.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from .cache import PageCache
from .config import Config
from .fetchers import COOLING_DOWN, Fetcher, FetchError, FetcherUnavailable
from .fetchers.paid import PaidFetcher
from .fetchers.plain import PlainFetcher
from .gate import BLOCKED, CHALLENGE, FETCH_ERROR, INTERACTIVE_CHALLENGE, Verdict, judge
from .page import FetchedPage
from .sites import SiteMemory, SiteState, Spacer, site_of


@dataclass(frozen=True)
class Attempt:
    fetcher: str
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

    @property
    def fetchers(self) -> list[str]:
        """Names of the Fetchers on the Ladder, in order: what a Caller may force."""
        return [f.name for f in self._ladder]

    def get(
        self, url: str, max_age_s: float | None = None, fetcher: str | None = None
    ) -> Success | Failure:
        """Content for `url`: from the cache if a copy is no older than `max_age_s`
        (default from config; 0 forces a live fetch), else from the Ladder. `fetcher`
        forces a live fetch to use only that Fetcher (a cache hit is still served).
        Raises UnknownFetcher if `fetcher` isn't on the Ladder."""
        if fetcher is not None and fetcher not in self.fetchers:
            raise UnknownFetcher(
                f"unknown Fetcher {fetcher!r}; expected one of: {', '.join(self.fetchers)}"
            )
        cached = self._cache.get(url, max_age_s)
        if cached is not None:
            return Success(
                page=cached.page,
                fetcher=cached.fetcher,
                verdict=cached.verdict,
                attempts=[],
                cached_at=cached.fetched_at,
            )
        site = site_of(url)
        state = self._sites.lookup(site)
        attempts: list[Attempt] = []
        for step in self._plan(state, fetcher):
            if step.home_connection and state.cooldown_until is not None:
                attempts.append(_cooling_down(step, site, state))
                continue
            attempt, page = self._try(step, url, site)
            attempts.append(attempt)
            if step.home_connection and _is_block(attempt):
                state = self._sites.blocked(site, attempt.verdict.reason, state)
            if page is not None and attempt.verdict.decision != "rejected":
                self._sites.succeeded(site, step.name)
                self._cache.put(url, page, step.name, attempt.verdict)
                return Success(page, step.name, attempt.verdict, attempts)
        return Failure(attempts)

    def _plan(self, state: SiteState, forced: str | None) -> list[Fetcher]:
        """The Fetchers to try, in order: only the forced one, or the Ladder starting
        at the Fetcher that last succeeded on the site, then the rest in Ladder order."""
        if forced is not None:
            return [f for f in self._ladder if f.name == forced]
        first = [f for f in self._ladder if f.name == state.remembered]
        return first + [f for f in self._ladder if f.name != state.remembered]

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
    return attempt.status_code in _BLOCK_STATUSES or attempt.verdict.reason in _BLOCK_REASONS


def _cooling_down(fetcher: Fetcher, site: str, state: SiteState) -> Attempt:
    until = datetime.fromtimestamp(state.cooldown_until or 0, UTC)
    message = (
        f"{fetcher.name} skipped: {site} is on a cool-down until"
        f" {until.isoformat(timespec='seconds').replace('+00:00', 'Z')}"
        f" after blocking the home connection ({state.cooldown_reason})"
    )
    return Attempt(
        fetcher.name, Verdict("rejected", COOLING_DOWN, message), None, unavailable=message
    )
