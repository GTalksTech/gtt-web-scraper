"""The Canary Set: trusted, content-rich URLs with known expected Verdicts, run daily
and before every deploy. Each one is asked of a running service through its
Firecrawl-compatible scrape endpoint, exactly as a Caller would, with a live fetch.

Each entry forces one Fetcher (the plain download unless it says otherwise), so a run
never spends the Paid Fetcher's quota and a known-bad page can't escalate to it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, get_args

import yaml

from ..sites import site_of

Expected = Literal["accepted", "suspect", "rejected"]
# What a run can get back: a Verdict, or "error" when the service didn't answer.
ERROR = "error"
# Forces no Fetcher: the request goes through Site Adapters and the whole Ladder.
AUTO = "auto"
# Markdown kept as evidence of what a page came back as.
SAMPLE_CHARS = 500


@dataclass(frozen=True)
class CanaryEntry:
    url: str
    expected: Expected
    # The Verdict reason expected too, e.g. "http_404" for a known missing page.
    reason: str | None = None
    # The Fetcher to force, or "auto" for none.
    fetcher: str = "plain"


def load_canary_set(path: Path) -> list[CanaryEntry]:
    """The Canary Set in the YAML file at `path`: a list of url/expected entries."""
    raw = yaml.safe_load(path.read_text()) or []
    if not isinstance(raw, list):
        raise ValueError(f"{path}: the Canary Set must be a list of entries")
    entries: list[CanaryEntry] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("url") or not item.get("expected"):
            raise ValueError(f"{path}: every Canary entry needs a url and an expected Verdict: {item!r}")
        expected = item["expected"]
        if expected not in get_args(Expected):
            raise ValueError(
                f"{path}: {item['url']}: expected must be one of {', '.join(get_args(Expected))}"
            )
        url = str(item["url"])
        if not site_of(url):
            raise ValueError(f"{path}: {url!r} is not a URL")
        entries.append(
            CanaryEntry(
                url=url,
                expected=expected,
                reason=item.get("reason") or None,
                fetcher=str(item.get("fetcher") or "plain"),
            )
        )
    urls = [e.url for e in entries]
    duplicates = sorted({u for u in urls if urls.count(u) > 1})
    if duplicates:
        raise ValueError(f"{path}: Canary URLs listed twice: {', '.join(duplicates)}")
    return entries


@dataclass(frozen=True)
class CanaryResult:
    entry: CanaryEntry
    verdict: str  # accepted, suspect, rejected, or "error"
    reason: str
    detail: str
    fetcher: str | None
    status_code: int | None
    sample: str  # the start of the markdown, when content came back

    @property
    def passed(self) -> bool:
        if self.verdict != self.entry.expected:
            return False
        return self.entry.reason is None or self.reason == self.entry.reason

    def evidence(self) -> dict[str, Any]:
        return {
            "url": self.entry.url,
            "expected": self.entry.expected,
            "expectedReason": self.entry.reason,
            "verdict": self.verdict,
            "reason": self.reason,
            "detail": self.detail,
            "fetcher": self.fetcher,
            "statusCode": self.status_code,
            "sample": self.sample,
        }


@dataclass(frozen=True)
class CanaryReport:
    target: str  # the service the Canary Set was run against
    results: list[CanaryResult]

    @property
    def passed(self) -> int:
        return sum(r.passed for r in self.results)

    @property
    def failures(self) -> list[CanaryResult]:
        return [r for r in self.results if not r.passed]

    def to_json(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "passed": self.passed,
            "total": len(self.results),
            "failures": [r.evidence() for r in self.failures],
        }


class ServiceUnreachable(Exception):
    """The service didn't answer at all (refused, timed out)."""


class Service(Protocol):
    """A running gtt-web-scraper, spoken to over its HTTP API."""

    @property
    def name(self) -> str: ...

    def post(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]: ...

    def get(self, path: str) -> tuple[int, dict[str, Any]]: ...


class HttpService:
    """A service at a base URL, such as http://127.0.0.1:3002."""

    # Long enough for a page whose challenge is polled; short enough that a hung
    # service can't hold up a deploy for long.
    def __init__(self, base_url: str, timeout_s: float = 90.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    @property
    def name(self) -> str:
        return self._base_url

    def post(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        request = urllib.request.Request(
            self._base_url + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._send(request)

    def get(self, path: str) -> tuple[int, dict[str, Any]]:
        return self._send(urllib.request.Request(self._base_url + path))

    def _send(self, request: urllib.request.Request) -> tuple[int, dict[str, Any]]:
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                return response.status, _json(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _json(exc.read())
        except (OSError, ValueError) as exc:
            raise ServiceUnreachable(f"{self._base_url} didn't answer: {exc}") from exc


def _json(raw: bytes) -> dict[str, Any]:
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def run_canary(entries: list[CanaryEntry], service: Service) -> CanaryReport:
    """Ask `service` for every Canary URL with a live fetch, and record what came back."""
    return CanaryReport(service.name, [_check(entry, service) for entry in entries])


def _check(entry: CanaryEntry, service: Service) -> CanaryResult:
    request: dict[str, Any] = {"url": entry.url, "formats": ["markdown"], "maxAge": 0}
    if entry.fetcher != AUTO:
        request["fetcher"] = entry.fetcher
    try:
        status, body = service.post("/v1/scrape", request)
    except ServiceUnreachable as exc:
        return CanaryResult(entry, ERROR, "service_error", str(exc), None, None, "")
    if status == 200 and body.get("success"):
        data: dict[str, Any] = body.get("data") or {}
        last = _last_attempt(data)
        return CanaryResult(
            entry,
            verdict=str(data.get("verdict")),
            reason=str(last.get("reason") or "ok"),
            detail=str(last.get("detail") or ""),
            fetcher=data.get("fetcher"),
            status_code=(data.get("metadata") or {}).get("statusCode"),
            sample=str(data.get("markdown") or "")[:SAMPLE_CHARS],
        )
    if status == 502:
        # No Fetcher produced real content: the last attempt says why.
        last = _last_attempt(body)
        return CanaryResult(
            entry,
            verdict="rejected",
            reason=str(last.get("reason") or "unknown"),
            detail=str(last.get("detail") or body.get("error") or ""),
            fetcher=last.get("fetcher"),
            status_code=last.get("statusCode"),
            sample="",
        )
    error = body.get("error") or body.get("detail") or "no error given"
    return CanaryResult(
        entry, ERROR, "service_error", f"HTTP {status}: {error}", None, None, ""
    )


def _last_attempt(body: dict[str, Any]) -> dict[str, Any]:
    attempts = body.get("attempts") or []
    last = attempts[-1] if attempts else {}
    return last if isinstance(last, dict) else {}
