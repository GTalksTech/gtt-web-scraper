"""Rolling success rates per Fetcher, Search Provider and Site Adapter, kept in the
service's SQLite file.

Each attempt is recorded under its subject (a Fetcher, a Search Provider or a Site
Adapter) and name, as a success, a failure, or unavailable:

- A Fetcher on the Ladder succeeds when its result is accepted or suspect, and fails
  when it is rejected or no page came back. The Paid Fetcher with its quota exhausted
  is unavailable, with the reason.
- A Site Adapter is judged the same way as a Fetcher.
- A Search Provider succeeds when it answers with results, and fails when it errors or
  finds nothing. One with its quota used up, or no key set, is unavailable.

What wasn't tried isn't recorded: a Fetcher or adapter skipped because its Site is on a
cool-down, a search served from the cache. Neither is a page the site says doesn't
exist (404, 410): the Fetcher got the site's real answer.

As with the page cache, database trouble never costs the Caller a page or a search: it
is logged and the attempt goes uncounted.
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
# What a rate is kept for. Also the key naming it in the rate's JSON.
Subject = Literal["fetcher", "provider", "adapter"]

# Attempts older than this are dropped: well past any window a rate is counted over,
# and long enough that the monthly summary, run on the 1st, still has all of last month.
RETENTION_S = 62 * 86400.0

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS outcomes (
        subject TEXT NOT NULL,
        name TEXT NOT NULL,
        at REAL NOT NULL,
        outcome TEXT NOT NULL,
        reason TEXT
    )
    """,
    # Pruning reads by time; a subject's latest outcome reads by subject, name, then time.
    "CREATE INDEX IF NOT EXISTS outcomes_at ON outcomes (at)",
    "CREATE INDEX IF NOT EXISTS outcomes_subject_name_at ON outcomes (subject, name, at)",
)


def _carry_over_fetcher_outcomes(db: sqlite3.Connection) -> None:
    """Move the Fetchers' outcomes from the table the previous version kept them in
    (Fetchers only) into `outcomes`, once, so an upgrade doesn't reset their rates."""
    old = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fetcher_outcomes'"
    ).fetchone()
    if old is None:
        return
    db.execute(
        "INSERT INTO outcomes (subject, name, at, outcome, reason)"
        " SELECT 'fetcher', fetcher, at, outcome, reason FROM fetcher_outcomes"
    )
    db.execute("DROP TABLE fetcher_outcomes")


@dataclass(frozen=True)
class SuccessRate:
    subject: Subject
    name: str
    attempts: int  # successes and failures; unavailable ones aren't attempts
    successes: int
    unavailable: int = 0
    # The reason the latest recorded outcome was "unavailable", or None if the latest
    # one was an attempt (so it is usable again).
    last_unavailable: str | None = None

    @property
    def success_rate(self) -> float | None:
        return self.successes / self.attempts if self.attempts else None

    def to_json(self) -> dict[str, Any]:
        rate = self.success_rate
        return {
            self.subject: self.name,
            "attempts": self.attempts,
            "successes": self.successes,
            "successRate": None if rate is None else round(rate, 4),
            "unavailable": self.unavailable,
            "lastUnavailable": self.last_unavailable,
        }

    @classmethod
    def from_json(cls, subject: Subject, data: dict[str, Any]) -> SuccessRate:
        return cls(
            subject=subject,
            name=str(data[subject]),
            attempts=int(data.get("attempts") or 0),
            successes=int(data.get("successes") or 0),
            unavailable=int(data.get("unavailable") or 0),
            last_unavailable=data.get("lastUnavailable") or None,
        )


class SuccessRates:
    """The record of attempts, shared by the Router (Fetchers, Site Adapters) and the
    Searcher (Search Providers) through the service's SQLite file."""

    def __init__(self, path: Path, clock: Callable[[], float]) -> None:
        self._path = path
        self._clock = clock
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with connect(self._path) as db:
            for statement in _SCHEMA:
                db.execute(statement)
            _carry_over_fetcher_outcomes(db)

    def record(
        self, subject: Subject, name: str, outcome: Outcome, reason: str | None = None
    ) -> None:
        now = self._clock()
        try:
            with connect(self._path) as db:
                db.execute(
                    "INSERT INTO outcomes (subject, name, at, outcome, reason)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (subject, name, now, outcome, reason),
                )
                db.execute("DELETE FROM outcomes WHERE at < ?", (now - RETENTION_S,))
        except sqlite3.Error:
            log.exception("success rates unwritable; %s %s attempt not counted", subject, name)

    def rates(self, subject: Subject, names: list[str], window_s: float) -> list[SuccessRate]:
        """Each of `names`' rate over the last `window_s`, in the order given."""
        now = self._clock()
        return self.between(subject, names, now - window_s, now + 1)

    def between(
        self, subject: Subject, names: list[str], since: float, until: float
    ) -> list[SuccessRate]:
        """Each of `names`' rate from `since` to `until` (exclusive), in the order given."""
        try:
            with connect(self._path) as db:
                counts = {
                    (name, outcome): n
                    for name, outcome, n in db.execute(
                        "SELECT name, outcome, COUNT(*) FROM outcomes"
                        " WHERE subject = ? AND at >= ? AND at < ? GROUP BY name, outcome",
                        (subject, since, until),
                    )
                }
                latest = {
                    name: (outcome, reason)
                    for name, outcome, reason in db.execute(
                        "SELECT name, outcome, reason FROM outcomes AS o"
                        " WHERE subject = ? AND at >= ? AND at < ? AND rowid = (SELECT rowid"
                        "  FROM outcomes WHERE subject = o.subject AND name = o.name AND at < ?"
                        "  ORDER BY at DESC, rowid DESC LIMIT 1)",
                        (subject, since, until, until),
                    )
                }
        except sqlite3.Error:
            log.exception("success rates unreadable")
            counts, latest = {}, {}
        rates = []
        for name in names:
            successes = counts.get((name, "success"), 0)
            outcome, reason = latest.get(name, (None, None))
            rates.append(
                SuccessRate(
                    subject=subject,
                    name=name,
                    attempts=successes + counts.get((name, "failure"), 0),
                    successes=successes,
                    unavailable=counts.get((name, "unavailable"), 0),
                    last_unavailable=reason if outcome == "unavailable" else None,
                )
            )
        return rates
