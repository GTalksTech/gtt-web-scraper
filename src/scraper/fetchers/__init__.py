"""Fetchers: the ways of getting a page. Each one is a step on the Ladder."""

from __future__ import annotations

from typing import Protocol

from ..page import FetchedPage


class FetchError(Exception):
    """The Fetcher got no response at all (DNS, connection, timeout)."""


class Fetcher(Protocol):
    name: str

    def fetch(self, url: str) -> FetchedPage: ...
