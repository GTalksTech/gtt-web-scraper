"""The page cache: results that passed the Content Gate, kept in SQLite by URL.

Only accepted and suspect results are stored; a failure is never cached, and a failed
live fetch leaves the last good copy in place.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from .gate import Verdict
from .page import FetchedPage

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
    def __init__(self, path: Path, clock: Callable[[], float]) -> None:
        self._path = path
        self._clock = clock
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """A connection for one operation, committed and closed after it. One per
        operation because requests run on FastAPI's thread pool."""
        with closing(sqlite3.connect(self._path, timeout=10)) as db, db:
            yield db

    def get(self, url: str, max_age_s: float) -> CachedPage | None:
        """The cached page for `url` if it is no older than `max_age_s`."""
        if max_age_s <= 0:
            return None
        with self._connect() as db:
            row = db.execute(
                "SELECT fetched_at, fetcher, verdict, page FROM pages WHERE url = ?", (url,)
            ).fetchone()
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

    def put(self, url: str, page: FetchedPage, fetcher: str, verdict: Verdict) -> float:
        """Store a result that passed the Content Gate. Returns when it was stored."""
        if verdict.decision == "rejected":
            raise ValueError("rejected results are never cached")
        now = self._clock()
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO pages (url, fetched_at, fetcher, verdict, page)"
                " VALUES (?, ?, ?, ?, ?)",
                (url, now, fetcher, json.dumps(asdict(verdict)), json.dumps(asdict(page))),
            )
        return now
