"""Fetchers: the ways of getting a page. Each one is a step on the Ladder."""

from __future__ import annotations

from typing import Protocol

from ..page import FetchedPage


class FetchError(Exception):
    """The Fetcher produced no page to judge (DNS, connection, timeout, a service error)."""


# Why a Fetcher is unavailable; shown as the reason on its attempt.
QUOTA_EXHAUSTED = "quota_exhausted"
NOT_CONFIGURED = "not_configured"


class FetcherUnavailable(FetchError):
    """The Fetcher can't be used at all right now (e.g. the Paid Fetcher's quota is
    exhausted), so it wasn't tried. `reason` is one of the reasons above; the message
    is for the Caller."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class Fetcher(Protocol):
    name: str
    # Whether a non-interactive challenge is worth re-fetching. False for a Fetcher
    # that spends quota on every fetch.
    polls_challenges: bool

    def fetch(self, url: str) -> FetchedPage: ...
