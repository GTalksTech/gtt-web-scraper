"""The page cache: results that passed the Content Gate, kept in SQLite by URL.

Only accepted and suspect results are stored; a failure is never cached, and a failed
live fetch leaves the last good copy in place. Cache trouble (a locked, full or damaged
database) never costs the Caller a page: it is logged and the request fetches live.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass

from .config import CacheConfig
from .db import connect
from .gate import Verdict
from .page import FetchedPage

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    url TEXT PRIMARY KEY,
    fetched_at REAL NOT NULL,
    fetcher TEXT NOT NULL,
    verdict TEXT NOT NULL,
    page TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class CachedPage:
    page: FetchedPage
    fetcher: str
    verdict: Verdict
    fetched_at: float  # seconds since the epoch


class PageCache:
    def __init__(self, config: CacheConfig, clock: Callable[[], float]) -> None:
        self._config = config
        self._path = config.resolved_path()
        self._clock = clock
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with connect(self._path) as db:
            db.execute(_SCHEMA)

    def get(self, url: str, max_age_s: float | None = None) -> CachedPage | None:
        """The cached page for `url` if it is no older than `max_age_s` (default from
        config; 0 or less never hits, forcing a live fetch)."""
        if max_age_s is None:
            max_age_s = self._config.default_max_age_s
        if max_age_s <= 0:
            return None
        try:
            with connect(self._path) as db:
                row = db.execute(
                    "SELECT fetched_at, fetcher, verdict, page FROM pages WHERE url = ?", (url,)
                ).fetchone()
        except sqlite3.Error:
            log.exception("page cache unreadable; fetching %s live", url)
            return None
        if row is None:
            return None
        fetched_at, fetcher, verdict, page = row
        if self._clock() - fetched_at > max_age_s:
            return None
        return CachedPage(
            page=FetchedPage(**json.loads(page)),
            fetcher=fetcher,
            verdict=Verdict(**json.loads(verdict)),
            fetched_at=fetched_at,
        )

    def published_dates(self, urls: list[str]) -> dict[str, str]:
        """The Published date of each of `urls` that is cached with one, however old
        the copy (a page's publication date doesn't go stale)."""
        if not urls:
            return {}
        marks = ", ".join("?" * len(urls))
        try:
            with connect(self._path) as db:
                rows = db.execute(
                    f"SELECT url, json_extract(page, '$.published') FROM pages"  # noqa: S608
                    f" WHERE url IN ({marks})",
                    urls,
                ).fetchall()
        except sqlite3.Error:
            log.exception("page cache unreadable; no Published dates from it")
            return {}
        return {url: published for url, published in rows if isinstance(published, str)}

    def put(self, url: str, page: FetchedPage, fetcher: str, verdict: Verdict) -> None:
        """Store a result that passed the Content Gate."""
        if verdict.decision == "rejected":
            raise ValueError("rejected results are never cached")
        try:
            with connect(self._path) as db:
                db.execute(
                    "INSERT OR REPLACE INTO pages (url, fetched_at, fetcher, verdict, page)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        url,
                        self._clock(),
                        fetcher,
                        json.dumps(asdict(verdict)),
                        json.dumps(asdict(page)),
                    ),
                )
        except sqlite3.Error:
            log.exception("page cache unwritable; %s not cached", url)
