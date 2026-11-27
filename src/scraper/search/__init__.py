"""The search chain: a query goes to the Search Providers in configured order, behind a
24-hour cache, and comes back as a ranked list of results or an explicit failure naming
each Provider's error.

A Provider that fails, runs out of quota, has no key or finds nothing passes the query
to the next. Firecrawl is never a Search Provider: search spends no Paid Fetcher quota.

The cache keeps each Provider's answer as it came. Shaping that answer for the Caller
(`_present`) happens after the cache, on every search, so a change to how results are
ranked (the Source List, ticket #13) applies to cached searches at once.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from ..config import Config
from .cache import SearchCache
from .providers import ProviderError, SearchProvider, SearchResult, build_providers

__all__ = [
    "ProviderAttempt",
    "SearchFailure",
    "SearchResult",
    "SearchSuccess",
    "Searcher",
]


@dataclass(frozen=True)
class ProviderAttempt:
    """A Search Provider that was asked and gave nothing usable, and why."""

    provider: str
    error: str


@dataclass(frozen=True)
class SearchSuccess:
    results: list[SearchResult]
    # The Provider whose results these are.
    provider: str
    # Providers asked before it that failed; empty when served from the cache.
    attempts: list[ProviderAttempt]
    # When the search was made, if served from the cache; None for a live search.
    cached_at: float | None = None


@dataclass(frozen=True)
class SearchFailure:
    # Every Provider asked, each with its error.
    attempts: list[ProviderAttempt]


class Searcher:
    def __init__(self, config: Config, clock: Callable[[], float] = time.time) -> None:
        self._providers: list[SearchProvider] = build_providers(config.search)
        self._cache = SearchCache(
            config.cache.resolved_path(), config.search.cache_max_age_s, clock
        )

    @property
    def providers(self) -> list[str]:
        """The Search Providers' names, in the order a query tries them."""
        return [p.name for p in self._providers]

    def search(self, query: str, limit: int) -> SearchSuccess | SearchFailure:
        """Up to `limit` results for `query`: from the cache if the same search was made
        in the last 24 hours, else from the first Provider that answers with results."""
        cached = self._cache.get(query, limit)
        if cached is not None and cached.results:
            return SearchSuccess(
                results=self._present(cached.results, limit),
                provider=cached.results[0].provider,
                attempts=[],
                cached_at=cached.searched_at,
            )
        attempts: list[ProviderAttempt] = []
        for provider in self._providers:
            try:
                results = provider.search(query, limit)
            except ProviderError as exc:
                attempts.append(ProviderAttempt(provider.name, str(exc)))
                continue
            if not results:
                attempts.append(ProviderAttempt(provider.name, "found no results"))
                continue
            self._cache.put(query, limit, results)
            return SearchSuccess(self._present(results, limit), provider.name, attempts)
        return SearchFailure(attempts)

    def _present(self, results: list[SearchResult], limit: int) -> list[SearchResult]:
        """A Provider's results as the Caller sees them. Source List re-ranking and
        Published dates (ticket #13) go here."""
        return results[:limit]
