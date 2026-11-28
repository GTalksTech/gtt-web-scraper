"""The Canary Set: trusted, content-rich URLs with known expected Verdicts, and search
queries with a site their results must include, run daily and before every deploy.
Each is asked of a running service through its Firecrawl-compatible endpoints, exactly
as a Caller would, live (never from the cache).

Each page forces one Fetcher (the plain download unless it says otherwise), so a run
never spends the Paid Fetcher's quota and a known-bad page can't escalate to it. A page
that checks a Site Adapter forces that adapter instead, so a failing adapter can't hide
behind the Ladder, and a JavaScript-heavy page forces the Chrome Fetcher.

Each search is asked of every Search Provider the service has, one at a time, so a
failing Provider can't hide behind the next one in the chain.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Protocol, get_args

import yaml

from ..search.providers import NOT_CONFIGURED
from ..sites import site_of

Expected = Literal["accepted", "suspect", "rejected"]
# What a run can get back: a Verdict, or "error" when the service didn't answer.
ERROR: Final = "error"
# Forces no Fetcher: the request goes through Site Adapters and the whole Ladder.
AUTO = "auto"
# Markdown kept as evidence of what a page came back as.
SAMPLE_CHARS = 500
# Results asked for per Canary search, and result URLs kept as evidence.
SEARCH_LIMIT = 10


@dataclass(frozen=True)
class CanaryEntry:
    url: str
    expected: Expected
    # The Verdict reason expected too, e.g. "http_404" for a known missing page.
    reason: str | None = None
    # The Fetcher or Site Adapter to force, or "auto" for none.
    fetcher: str = "plain"


@dataclass(frozen=True)
class SearchCanary:
    query: str
    # A domain at least one result must come from (a subdomain counts).
    expect: str
    # The Search Provider to ask, or None for each of the service's in turn.
    provider: str | None = None


@dataclass(frozen=True)
class CanarySet:
    pages: list[CanaryEntry] = field(default_factory=list)
    searches: list[SearchCanary] = field(default_factory=list)


def load_canary_set(path: Path) -> CanarySet:
    """The Canary Set in the YAML file at `path`: a list of entries, each a page
    (url, expected) or a search (query, expect)."""
    raw = yaml.safe_load(path.read_text()) or []
    if not isinstance(raw, list):
        raise ValueError(f"{path}: the Canary Set must be a list of entries")
    canary = CanarySet()
    for item in raw:
        if isinstance(item, dict) and "query" in item:
            canary.searches.append(_search_entry(path, item))
        else:
            canary.pages.append(_page_entry(path, item))
    urls = [e.url for e in canary.pages]
    duplicates = sorted({u for u in urls if urls.count(u) > 1})
    if duplicates:
        raise ValueError(f"{path}: Canary URLs listed twice: {', '.join(duplicates)}")
    queries = [(s.query, s.provider) for s in canary.searches]
    repeated = sorted({q for q, p in queries if queries.count((q, p)) > 1})
    if repeated:
        raise ValueError(f"{path}: Canary searches listed twice: {', '.join(repeated)}")
    return canary


def _page_entry(path: Path, item: Any) -> CanaryEntry:
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
    return CanaryEntry(
        url=url,
        expected=expected,
        reason=item.get("reason") or None,
        fetcher=str(item.get("fetcher") or "plain"),
    )


def _search_entry(path: Path, item: dict[str, Any]) -> SearchCanary:
    query = str(item.get("query") or "").strip()
    expect = str(item.get("expect") or "").strip().lower()
    if not query or not expect:
        raise ValueError(
            f"{path}: every Canary search needs a query and a domain to expect: {item!r}"
        )
    if "/" in expect or "." not in expect:
        raise ValueError(f"{path}: {query!r}: expect must be a domain, like docs.python.org")
    provider = str(item.get("provider") or "").strip() or None
    return SearchCanary(query=query, expect=expect, provider=provider)


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
    def key(self) -> str:
        """Names this check across runs, so a live run and a candidate's line up."""
        return self.entry.url

    @property
    def passed(self) -> bool:
        if self.verdict != self.entry.expected:
            return False
        return self.entry.reason is None or self.reason == self.entry.reason

    @property
    def service_error(self) -> bool:
        return self.verdict == ERROR

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


# What a Canary search found: a result from the expected domain, results but none from
# it, no results because the Provider failed, or no answer from the service at all.
SearchOutcome = Literal["found", "missing", "failed", "error"]


@dataclass(frozen=True)
class SearchCheck:
    canary: SearchCanary
    # The Search Provider asked, or None when the whole chain was.
    provider: str | None
    outcome: SearchOutcome
    detail: str
    urls: list[str]  # the result URLs that came back

    @property
    def key(self) -> str:
        return f"search via {self.provider or 'the chain'}: {self.canary.query}"

    @property
    def passed(self) -> bool:
        return self.outcome == "found"

    @property
    def service_error(self) -> bool:
        return self.outcome == ERROR

    def evidence(self) -> dict[str, Any]:
        return {
            "query": self.canary.query,
            "expect": self.canary.expect,
            "provider": self.provider,
            "outcome": self.outcome,
            "detail": self.detail,
            "results": self.urls,
        }


@dataclass(frozen=True)
class CanaryReport:
    target: str  # the service the Canary Set was run against
    results: list[CanaryResult]  # the pages
    searches: list[SearchCheck] = field(default_factory=list)

    @property
    def checks(self) -> list[CanaryResult | SearchCheck]:
        return [*self.results, *self.searches]

    @property
    def total(self) -> int:
        return len(self.results) + len(self.searches)

    @property
    def passed(self) -> int:
        return sum(c.passed for c in self.checks)

    @property
    def failures(self) -> list[CanaryResult]:
        return [r for r in self.results if not r.passed]

    @property
    def search_failures(self) -> list[SearchCheck]:
        return [s for s in self.searches if not s.passed]


class ServiceUnreachable(Exception):
    """The service didn't answer at all (refused, timed out)."""


class Service(Protocol):
    """A running gtt-web-scraper, spoken to over its HTTP API."""

    @property
    def name(self) -> str: ...

    def post(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]: ...

    def get(self, path: str) -> tuple[int, dict[str, Any]]: ...


# Sent with every Canary request, so the service counts it as a check rather than a
# Caller's request (health/usage.py; the API reads it as `x_gtt_check`).
CHECK_HEADER = {"X-GTT-Check": "canary"}


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
            headers={"Content-Type": "application/json", **CHECK_HEADER},
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


def run_canary(canary: CanarySet, service: Service) -> CanaryReport:
    """Ask `service` for every Canary page with a live fetch and every Canary search
    with a live search, and record what came back."""
    pages = [_check(entry, service) for entry in canary.pages]
    providers = _providers(service) if any(s.provider is None for s in canary.searches) else []
    searches = []
    for search in canary.searches:
        # Each Provider in turn; the whole chain if the service didn't say which it has.
        asked: list[str | None] = [search.provider] if search.provider else [*providers] or [None]
        for provider in asked:
            check = _search(search, provider, service)
            if check is not None:
                searches.append(check)
    return CanaryReport(service.name, pages, searches)


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


def _providers(service: Service) -> list[str]:
    """The service's Search Providers, in order, or none if it won't say."""
    try:
        status, body = service.get("/health/providers")
    except ServiceUnreachable:
        return []
    if status != 200:
        return []
    return [str(r["provider"]) for r in body.get("providers") or [] if r.get("provider")]


def _search(
    canary: SearchCanary, provider: str | None, service: Service
) -> SearchCheck | None:
    """What asking `service` for `canary` through `provider` found, or None when that
    Provider has no key set: leaving one out is a choice, not a breakage."""
    request: dict[str, Any] = {"query": canary.query, "limit": SEARCH_LIMIT}
    if provider is not None:
        request["provider"] = provider
    try:
        status, body = service.post("/v1/search", request)
    except ServiceUnreachable as exc:
        return SearchCheck(canary, provider, ERROR, str(exc), [])
    if status == 200 and body.get("success"):
        urls = [str(r.get("url")) for r in body.get("data") or [] if isinstance(r, dict)]
        if any(_from(url, canary.expect) for url in urls):
            return SearchCheck(canary, provider, "found", "", urls)
        detail = f"no result from {canary.expect} among {len(urls)}"
        return SearchCheck(canary, provider, "missing", detail, urls)
    if status == 502:
        attempts = [a for a in body.get("attempts") or [] if isinstance(a, dict)]
        if provider is not None and [a.get("reason") for a in attempts] == [NOT_CONFIGURED]:
            return None
        errors = "; ".join(f"{a.get('provider')}: {a.get('error')}" for a in attempts)
        return SearchCheck(canary, provider, "failed", errors or str(body.get("error") or ""), [])
    error = body.get("error") or body.get("detail") or "no error given"
    return SearchCheck(canary, provider, ERROR, f"HTTP {status}: {error}", [])


def _from(url: str, domain: str) -> bool:
    site = site_of(url)
    return site == domain or site.endswith(f".{domain}")
