"""The search chain: a query goes to the Search Providers in configured order, behind a
24-hour cache, and comes back as a ranked list of results or an explicit failure naming
each Provider's error.

A Provider that fails, runs out of quota, has no key or finds nothing passes the query
to the next. Firecrawl is never a Search Provider: search spends no Paid Fetcher quota.

The cache keeps each Provider's answer as it came. Shaping that answer for the Caller
(`_present`) happens after the cache, on every search, so a change to how results are
ranked (the Source List) applies to cached searches at once.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from ..cache import PageCache
from ..config import Config
from ..dates import published_date
from .cache import SearchCache
from .providers import ProviderError, SearchProvider, SearchResult, build_providers
from .source_list import SourceRanking

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
        self.cache_max_age_s = config.search.cache_max_age_s
        self._cache = SearchCache(config.cache.resolved_path(), self.cache_max_age_s, clock)
        self._ranking = SourceRanking(config.source_list)
        self._pages = PageCache(config.cache, clock)
        self._clock = clock

    @property
    def providers(self) -> list[str]:
        """The Search Providers' names, in the order a query tries them."""
        return [p.name for p in self._providers]

    def search(self, query: str, limit: int) -> SearchSuccess | SearchFailure:
        """Up to `limit` results for `query`: from the cache if the same query was
        searched recently (24 hours by default) for at least as many results, else from
        the first Provider that answers with results."""
        cached = self._cache.get(query, limit)
        # A cached answer the Source List now blocks entirely is searched again.
        shown = self._present(cached.results, limit, cached.searched_at) if cached else []
        if cached is not None and shown:
            return SearchSuccess(
                results=shown,
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
            shown = self._present(results, limit, self._clock())
            if not shown:
                attempts.append(
                    ProviderAttempt(provider.name, "found only results from blocked domains")
                )
                continue
            self._cache.put(query, limit, results)
            return SearchSuccess(shown, provider.name, attempts)
        return SearchFailure(attempts)

    def _present(
        self, results: list[SearchResult], limit: int, searched_at: float
    ) -> list[SearchResult]:
        """A Provider's results as the Caller sees them: blocked domains dropped and
        trusted ones first (the Source List), trimmed to `limit`, each with its
        Published date as YYYY-MM-DD: the Provider's, read as of when it was searched,
        else the page's own when the page is in the page cache, else None (unknown)."""
        shown = self._ranking.rank(results)[:limit]
        dated = [replace(r, published=published_date(r.published, searched_at)) for r in shown]
        undated = [r.url for r in dated if r.published is None]
        from_pages = self._pages.published_dates(undated)
        return [
            replace(r, published=from_pages[r.url]) if r.url in from_pages else r for r in dated
        ]
