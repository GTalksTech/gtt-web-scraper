"""The Router owns a request end to end: climb the Ladder, judge each result, and
return either content or an explicit failure listing every Fetcher tried."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

from .config import Config
from .fetchers import Fetcher, FetchError
from .fetchers.plain import PlainFetcher
from .gate import CHALLENGE, Verdict, judge
from .page import FetchedPage


@dataclass(frozen=True)
class Attempt:
    fetcher: str
    verdict: Verdict
    status_code: int | None


@dataclass(frozen=True)
class Success:
    page: FetchedPage
    fetcher: str
    verdict: Verdict
    attempts: list[Attempt]


@dataclass(frozen=True)
class Failure:
    attempts: list[Attempt]


def build_ladder(config: Config) -> list[Fetcher]:
    available: dict[str, Fetcher] = {"plain": PlainFetcher(config.plain)}
    return [available[name] for name in config.ladder]


class Router:
    def __init__(self, config: Config, ladder: list[Fetcher] | None = None) -> None:
        self._config = config
        self._ladder = ladder if ladder is not None else build_ladder(config)

    def get(self, url: str) -> Success | Failure:
        attempts: list[Attempt] = []
        for fetcher in self._ladder:
            try:
                page, verdict = self._fetch_and_judge(fetcher, url)
            except FetchError as exc:
                failed = Verdict("rejected", "fetch_error", str(exc))
                attempts.append(Attempt(fetcher.name, failed, None))
                continue
            attempts.append(Attempt(fetcher.name, verdict, page.status))
            if verdict.decision != "rejected":
                return Success(page, fetcher.name, verdict, attempts)
        return Failure(attempts)

    def _fetch_and_judge(self, fetcher: Fetcher, url: str) -> tuple[FetchedPage, Verdict]:
        """One Fetcher's result, re-fetched briefly while a non-interactive challenge
        might clear. Interactive challenges are returned at once."""
        gate = self._config.gate
        page = fetcher.fetch(url)
        verdict = judge(page, gate)
        polls = 0
        while verdict.reason == CHALLENGE and polls < gate.challenge_poll_attempts:
            polls += 1
            time.sleep(gate.challenge_poll_interval_s)
            page = fetcher.fetch(url)
            verdict = judge(page, gate)
        if verdict.reason == CHALLENGE:
            verdict = replace(verdict, detail=f"{verdict.detail}; still challenged after {polls} polls")
        return page, verdict
