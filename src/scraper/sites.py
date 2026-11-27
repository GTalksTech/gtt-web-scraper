"""What the service knows about each site: when it may next be fetched over the home
connection, whether it is on a cool-down, and which Fetcher last worked there.

A site is a URL's host name. Spacing is kept in memory (it only matters for the next
few seconds); cool-downs and the remembered Fetcher are kept in SQLite so they survive
restarts. As with the page cache, database trouble never costs the Caller a page: it
is logged and the request goes ahead as if nothing were remembered.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from .config import SitesConfig
from .db import connect

log = logging.getLogger(__name__)


def site_of(url: str) -> str:
    """The site a URL belongs to: its host name, lower-cased."""
    return (urlsplit(url).hostname or "").lower()


class Spacer:
    """Keeps requests to one site at least `interval_s` apart. Each request reserves the
    next free slot for its site, then sleeps until it, so concurrent requests queue."""

    _PRUNE_ABOVE = 1024  # sites remembered before stale slots are dropped

    def __init__(self, interval_s: float) -> None:
        self._interval_s = interval_s
        self._lock = threading.Lock()
        self._next_free: dict[str, float] = {}

    def wait_turn(self, site: str) -> None:
        if self._interval_s <= 0:
            return
        with self._lock:
            now = time.monotonic()
            turn = max(now, self._next_free.get(site, now))
            self._next_free[site] = turn + self._interval_s
            if len(self._next_free) > self._PRUNE_ABOVE:
                self._next_free = {s: t for s, t in self._next_free.items() if t > now}
        if turn > now:
            time.sleep(turn - now)


@dataclass(frozen=True)
class SiteState:
    # The Fetcher that last succeeded on the site, while it is still trusted to go first.
    remembered: str | None = None
    # When the site's cool-down ends (seconds since the epoch), if it is on one.
    cooldown_until: float | None = None
    cooldown_reason: str | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    site TEXT PRIMARY KEY,
    fetcher TEXT,
    fetcher_since REAL,
    cooldown_until REAL,
    cooldown_reason TEXT
)
"""


class SiteMemory:
    def __init__(self, path: Path, config: SitesConfig, clock: Callable[[], float]) -> None:
        self._path = path
        self._config = config
        self._clock = clock
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with connect(self._path) as db:
            db.execute(_SCHEMA)

    def lookup(self, site: str) -> SiteState:
        try:
            with connect(self._path) as db:
                row = db.execute(
                    "SELECT fetcher, fetcher_since, cooldown_until, cooldown_reason"
                    " FROM sites WHERE site = ?",
                    (site,),
                ).fetchone()
        except sqlite3.Error:
            log.exception("site memory unreadable; treating %s as unknown", site)
            return SiteState()
        if row is None:
            return SiteState()
        fetcher, since, cooldown_until, cooldown_reason = row
        now = self._clock()
        trusted = fetcher is not None and now - since < self._config.memory_ttl_s
        cooling = cooldown_until is not None and now < cooldown_until
        return SiteState(
            remembered=fetcher if trusted else None,
            cooldown_until=cooldown_until if cooling else None,
            cooldown_reason=cooldown_reason if cooling else None,
        )

    def succeeded(self, site: str, fetcher: str) -> None:
        """Remember `fetcher` as the one that works on `site`. Its trust runs from when
        it first succeeded there, so repeated successes don't extend it forever."""
        now = self._clock()
        self._write(
            "INSERT INTO sites (site, fetcher, fetcher_since) VALUES (:site, :fetcher, :now)"
            " ON CONFLICT (site) DO UPDATE SET"
            " fetcher_since = CASE"
            "  WHEN fetcher IS :fetcher AND :now - fetcher_since < :ttl THEN fetcher_since"
            "  ELSE :now END,"
            " fetcher = :fetcher",
            {"site": site, "fetcher": fetcher, "now": now, "ttl": self._config.memory_ttl_s},
        )

    def blocked(self, site: str, reason: str, state: SiteState) -> SiteState:
        """Put `site` on a cool-down: it just blocked the home connection. Returns
        `state` (what was known about the site) with the cool-down added."""
        until = self._clock() + self._config.cooldown_s
        self._write(
            "INSERT INTO sites (site, cooldown_until, cooldown_reason)"
            " VALUES (:site, :until, :reason)"
            " ON CONFLICT (site) DO UPDATE SET"
            " cooldown_until = :until, cooldown_reason = :reason",
            {"site": site, "until": until, "reason": reason},
        )
        return replace(state, cooldown_until=until, cooldown_reason=reason)

    def _write(self, sql: str, params: dict[str, object]) -> None:
        try:
            with connect(self._path) as db:
                db.execute(sql, params)
        except sqlite3.Error:
            log.exception("site memory unwritable; %s not remembered", params["site"])
