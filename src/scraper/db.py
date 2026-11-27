"""The service's SQLite file, shared by the page cache and per-site memory."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    """A connection for one operation, committed and closed after it. One per
    operation because requests run on FastAPI's thread pool."""
    with closing(sqlite3.connect(path, timeout=10)) as db, db:
        yield db
