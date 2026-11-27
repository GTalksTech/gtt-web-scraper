"""What the service served, request by request, for the monthly summary: each page or
search request and what answered it (the cache, a Fetcher, a Site Adapter, a Search
Provider, or "failed" when nothing did).

The Canary Set's requests are checks, not Callers' requests: they are counted apart,
so the daily and pre-deploy runs don't drown out what Callers actually asked for.

As with the success rates, database trouble never costs the Caller a page or a search.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from ..db import connect
from .rates import RETENTION_S

log = logging.getLogger(__name__)

Kind = Literal["page", "search"]
CACHE = "cache"
FAILED = "failed"

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS requests (
        kind TEXT NOT NULL,
        served_by TEXT NOT NULL,
        is_check INTEGER NOT NULL,
        at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS requests_at ON requests (at)",
)


def month_bounds(month: str) -> tuple[float, float]:
    """The start and end (exclusive) of a month given as YYYY-MM, in UTC, as epoch
    seconds. Raises ValueError for anything else."""
    start = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    end = start.replace(year=start.year + start.month // 12, month=start.month % 12 + 1)
    return start.timestamp(), end.timestamp()


class RequestLog:
    def __init__(self, path: Path, clock: Callable[[], float]) -> None:
        self._path = path
        self._clock = clock
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with connect(self._path) as db:
            for statement in _SCHEMA:
                db.execute(statement)

    def record(self, kind: Kind, served_by: str, *, check: bool) -> None:
        now = self._clock()
        try:
            with connect(self._path) as db:
                db.execute(
                    "INSERT INTO requests (kind, served_by, is_check, at) VALUES (?, ?, ?, ?)",
                    (kind, served_by, int(check), now),
                )
                db.execute("DELETE FROM requests WHERE at < ?", (now - RETENTION_S,))
        except sqlite3.Error:
            log.exception("request log unwritable; a %s request not counted", kind)

    def between(self, start: float, end: float) -> dict[str, Any]:
        """Callers' requests from `start` to `end` (exclusive) by kind and what served
        them, most first, and the Canary Set's checks by kind."""
        try:
            with connect(self._path) as db:
                rows = db.execute(
                    "SELECT kind, served_by, is_check, COUNT(*) AS n FROM requests"
                    " WHERE at >= ? AND at < ? GROUP BY kind, served_by, is_check"
                    " ORDER BY n DESC, served_by",
                    (start, end),
                ).fetchall()
        except sqlite3.Error:
            log.exception("request log unreadable")
            rows = []
        served: dict[str, dict[str, int]] = {"page": {}, "search": {}}
        checks = {"page": 0, "search": 0}
        for kind, served_by, is_check, n in rows:
            if kind not in served:
                continue
            if is_check:
                checks[kind] += n
            else:
                served[kind][served_by] = n
        return {
            "pages": {"requests": sum(served["page"].values()), "servedBy": served["page"]},
            "searches": {"requests": sum(served["search"].values()), "servedBy": served["search"]},
            "checks": {"pages": checks["page"], "searches": checks["search"]},
        }
