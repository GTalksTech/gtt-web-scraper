"""Rolling success rates per Fetcher, kept in the service's SQLite file.

Every attempt a Fetcher on the Ladder makes is recorded as a success (its result was
accepted or suspect) or a failure (rejected, or no page at all). A Fetcher that
couldn't be used (the Paid Fetcher with its quota exhausted) is recorded apart, as
unavailable with the reason. A Fetcher skipped because its Site is on a cool-down
isn't recorded: it wasn't tried and it wasn't the Fetcher's fault.

As with the page cache, database trouble never costs the Caller a page: it is logged
and the attempt goes uncounted.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..db import connect

log = logging.getLogger(__name__)

Outcome = Literal["success", "failure", "unavailable"]

# Attempts older than this are dropped; well past any window a rate is counted over.
RETENTION_S = 30 * 86400.0

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS fetcher_outcomes (
        fetcher TEXT NOT NULL,
        at REAL NOT NULL,
        outcome TEXT NOT NULL,
        reason TEXT
    )
    """,
    # Pruning reads by time; a Fetcher's latest outcome reads by Fetcher, then time.
    "CREATE INDEX IF NOT EXISTS fetcher_outcomes_at ON fetcher_outcomes (at)",
    "CREATE INDEX IF NOT EXISTS fetcher_outcomes_fetcher_at ON fetcher_outcomes (fetcher, at)",
)


@dataclass(frozen=True)
class FetcherRate:
    fetcher: str
    attempts: int  # successes and failures; unavailable ones aren't attempts
    successes: int
    unavailable: int
    # The reason the latest recorded outcome was "unavailable", or None if the latest
    # one was an attempt (so the Fetcher is usable again).
    last_unavailable: str | None

    @property
    def success_rate(self) -> float | None:
        return self.successes / self.attempts if self.attempts else None

    def to_json(self) -> dict[str, Any]:
        rate = self.success_rate
        return {
            "fetcher": self.fetcher,
            "attempts": self.attempts,
            "successes": self.successes,
            "successRate": None if rate is None else round(rate, 4),
            "unavailable": self.unavailable,
            "lastUnavailable": self.last_unavailable,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> FetcherRate:
        return cls(
            fetcher=str(data["fetcher"]),
            attempts=int(data.get("attempts") or 0),
            successes=int(data.get("successes") or 0),
            unavailable=int(data.get("unavailable") or 0),
            last_unavailable=data.get("lastUnavailable") or None,
        )


class FetcherHealth:
    def __init__(self, path: Path, clock: Callable[[], float]) -> None:
        self._path = path
        self._clock = clock
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with connect(self._path) as db:
            for statement in _SCHEMA:
                db.execute(statement)

    def record(self, fetcher: str, outcome: Outcome, reason: str | None = None) -> None:
        now = self._clock()
        try:
            with connect(self._path) as db:
                db.execute(
                    "INSERT INTO fetcher_outcomes (fetcher, at, outcome, reason)"
                    " VALUES (?, ?, ?, ?)",
                    (fetcher, now, outcome, reason),
                )
                db.execute("DELETE FROM fetcher_outcomes WHERE at < ?", (now - RETENTION_S,))
        except sqlite3.Error:
            log.exception("fetcher health unwritable; %s attempt not counted", fetcher)

    def rates(self, fetchers: list[str], window_s: float) -> list[FetcherRate]:
        """Each of `fetchers`' rate over the last `window_s`, in the order given."""
        since = self._clock() - window_s
        try:
            with connect(self._path) as db:
                counts = {
                    (fetcher, outcome): n
                    for fetcher, outcome, n in db.execute(
                        "SELECT fetcher, outcome, COUNT(*) FROM fetcher_outcomes"
                        " WHERE at >= ? GROUP BY fetcher, outcome",
                        (since,),
                    )
                }
                latest = {
                    fetcher: (outcome, reason)
                    for fetcher, outcome, reason in db.execute(
                        "SELECT fetcher, outcome, reason FROM fetcher_outcomes AS o"
                        " WHERE at >= ? AND rowid = (SELECT rowid FROM fetcher_outcomes"
                        "  WHERE fetcher = o.fetcher ORDER BY at DESC, rowid DESC LIMIT 1)",
                        (since,),
                    )
                }
        except sqlite3.Error:
            log.exception("fetcher health unreadable")
            counts, latest = {}, {}
        rates = []
        for fetcher in fetchers:
            successes = counts.get((fetcher, "success"), 0)
            outcome, reason = latest.get(fetcher, (None, None))
            rates.append(
                FetcherRate(
                    fetcher=fetcher,
                    attempts=successes + counts.get((fetcher, "failure"), 0),
                    successes=successes,
                    unavailable=counts.get((fetcher, "unavailable"), 0),
                    last_unavailable=reason if outcome == "unavailable" else None,
                )
            )
        return rates
