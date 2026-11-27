"""The search cache: a query's results, kept in SQLite (the same file as the page cache)
so repeated searches don't spend free Provider quota.

One entry per query, whatever the limit: a search for up to N results is served from an
entry made for N or more (Hermes's v1 default of 5 and Claude Code's 10 share one), and
a search for more replaces it. Only a successful search is stored, and expired entries
are dropped as new ones are written. Cache trouble (a locked, full or damaged database)
never costs the Caller a search: it is logged and the query goes to the Providers.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from ..db import connect
from .providers import SearchResult

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    query TEXT PRIMARY KEY,
    -- The limit the search was made with; it may have found fewer.
    max_results INTEGER NOT NULL,
    searched_at REAL NOT NULL,
    results TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class CachedSearch:
    results: list[SearchResult]
    searched_at: float  # seconds since the epoch


def cache_key(query: str) -> str:
    """Queries that differ only in spacing or case are the same search."""
    return " ".join(query.split()).casefold()


class SearchCache:
    def __init__(self, path: Path, max_age_s: float, clock: Callable[[], float]) -> None:
        self._path = path
        self._max_age_s = max_age_s
        self._clock = clock
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with connect(self._path) as db:
            db.execute(_SCHEMA)

    def get(self, query: str, limit: int) -> CachedSearch | None:
        """The cached results for `query` if they are recent and were searched for at
        least `limit` results (all of them; the Caller trims to its limit)."""
        try:
            with connect(self._path) as db:
                row = db.execute(
                    "SELECT max_results, searched_at, results FROM searches WHERE query = ?",
                    (cache_key(query),),
                ).fetchone()
        except sqlite3.Error:
            log.exception("search cache unreadable; searching %r live", query)
            return None
        if row is None:
            return None
        max_results, searched_at, results = row
        if max_results < limit or self._clock() - searched_at > self._max_age_s:
            return None
        return CachedSearch(
            results=[SearchResult(**r) for r in json.loads(results)], searched_at=searched_at
        )

    def put(self, query: str, limit: int, results: list[SearchResult]) -> None:
        now = self._clock()
        try:
            with connect(self._path) as db:
                db.execute(
                    "DELETE FROM searches WHERE searched_at < ?", (now - self._max_age_s,)
                )
                db.execute(
                    "INSERT OR REPLACE INTO searches (query, max_results, searched_at, results)"
                    " VALUES (?, ?, ?, ?)",
                    (cache_key(query), limit, now, json.dumps([asdict(r) for r in results])),
                )
        except sqlite3.Error:
            log.exception("search cache unwritable; %r not cached", query)
