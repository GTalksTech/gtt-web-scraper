"""The Router owns a request end to end: climb the Ladder, judge each result, and
return either content or an explicit failure listing every Fetcher tried."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

from .config import Config
from .fetchers import Fetcher, FetchError, FetcherUnavailable
from .fetchers.paid import PaidFetcher
from .fetchers.plain import PlainFetcher
from .gate import CHALLENGE, FETCH_ERROR, Verdict, judge
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


def _build_ladder(config: Config) -> list[Fetcher]:
    available: dict[str, Fetcher] = {
        "plain": PlainFetcher(config.plain),
        "paid": PaidFetcher(config.paid),
    }
    return [available[name] for name in config.ladder]


class Router:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._ladder = _build_ladder(config)

    def get(self, url: str) -> Success | Failure:
        attempts: list[Attempt] = []
        for fetcher in self._ladder:
            attempt, page = self._try(fetcher, url)
            attempts.append(attempt)
            if page is not None and attempt.verdict.decision != "rejected":
                return Success(page, fetcher.name, attempt.verdict, attempts)
        return Failure(attempts)

    def _try(self, fetcher: Fetcher, url: str) -> tuple[Attempt, FetchedPage | None]:
        """One Fetcher's result, re-fetched briefly while a non-interactive challenge
        might clear (unless the Fetcher spends quota per fetch). Interactive challenges
        are returned at once."""
        gate = self._config.gate
        try:
            page = fetcher.fetch(url)
        except FetcherUnavailable as exc:
            return Attempt(fetcher.name, Verdict("rejected", exc.reason, str(exc)), None), None
        except FetchError as exc:
            return Attempt(fetcher.name, Verdict("rejected", FETCH_ERROR, str(exc)), None), None
        verdict = judge(page, gate)
        polls = 0
        max_polls = gate.challenge_poll_attempts if fetcher.polls_challenges else 0
        while verdict.reason == CHALLENGE and polls < max_polls:
            polls += 1
            time.sleep(gate.challenge_poll_interval_s)
            try:
                page = fetcher.fetch(url)
            except FetchError:
                continue  # keep the challenge Verdict as the cause
            verdict = judge(page, gate)
        if verdict.reason == CHALLENGE:
            verdict = replace(verdict, detail=f"{verdict.detail}; still challenged after {polls} polls")
        return Attempt(fetcher.name, verdict, page.status), page
