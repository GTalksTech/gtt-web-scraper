"""The Router owns a request end to end: climb the Ladder, judge each result, and
return either content or an explicit failure listing every Fetcher tried."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from .cache import PageCache
from .config import Config
from .fetchers import Fetcher, FetchError, FetcherUnavailable
from .fetchers.paid import PaidFetcher
from .fetchers.plain import PlainFetcher
from .gate import CHALLENGE, FETCH_ERROR, Verdict, judge
from .page import FetchedPage


@dataclass(frozen=True)
class Attempt:
    fetcher: str
    verdict: Verdict
    status_code: int | None
    # Set when the Fetcher couldn't be used at all, e.g. "Paid Fetcher unavailable:
    # quota exhausted".
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

    def get(self, url: str, max_age_s: float | None = None) -> Success | Failure:
        """Content for `url`: from the cache if a copy is no older than `max_age_s`
        (default from config; 0 forces a live fetch), else from the Ladder."""
        cached = self._cache.get(url, max_age_s)
        if cached is not None:
            return Success(
                page=cached.page,
                fetcher=cached.fetcher,
                verdict=cached.verdict,
                attempts=[],
                cached_at=cached.fetched_at,
            )
        attempts: list[Attempt] = []
        for fetcher in self._ladder:
            attempt, page = self._try(fetcher, url)
            attempts.append(attempt)
            if page is not None and attempt.verdict.decision != "rejected":
                self._cache.put(url, page, fetcher.name, attempt.verdict)
                return Success(page, fetcher.name, attempt.verdict, attempts)
        return Failure(attempts)

    def _try(self, fetcher: Fetcher, url: str) -> tuple[Attempt, FetchedPage | None]:
        """One Fetcher's result, re-fetched briefly while a non-interactive challenge
        might clear (unless the Fetcher spends quota per fetch). Interactive challenges
        are returned at once."""
        gate = self._config.gate
        try:
            page = fetcher.fetch(url)
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
                page = fetcher.fetch(url)
            except FetchError:
                continue  # keep the challenge Verdict as the cause
            verdict = judge(page, gate)
        if verdict.reason == CHALLENGE:
            verdict = replace(verdict, detail=f"{verdict.detail}; still challenged after {polls} polls")
        return Attempt(fetcher.name, verdict, page.status), page
