"""Shaping a Provider's results with the Source List: results from blocked domains are
dropped and results from trusted domains move ahead of the rest, each group keeping the
Provider's order. An entry covers its domain and every subdomain; blocked beats trusted.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from ..config import SourceList
from .providers import SearchResult

Standing = Literal["trusted", "blocked"]


def _suffixes(url: str) -> list[str]:
    """The URL's host and each parent domain ("a.b.org" → a.b.org, b.org, org)."""
    try:
        host = (urlsplit(url).hostname or "").rstrip(".")
    except ValueError:
        return []
    labels = host.split(".")
    return [".".join(labels[i:]) for i in range(len(labels))] if host else []


class SourceRanking:
    def __init__(self, source_list: SourceList) -> None:
        self._trusted = frozenset(source_list.trusted)
        self._blocked = frozenset(source_list.blocked)

    def standing(self, url: str) -> Standing | None:
        suffixes = _suffixes(url)
        if any(s in self._blocked for s in suffixes):
            return "blocked"
        if any(s in self._trusted for s in suffixes):
            return "trusted"
        return None

    def rank(self, results: list[SearchResult]) -> list[SearchResult]:
        """`results` without blocked domains, trusted ones first."""
        standings = [(self.standing(r.url), r) for r in results]
        trusted = [r for s, r in standings if s == "trusted"]
        others = [r for s, r in standings if s is None]
        return trusted + others
