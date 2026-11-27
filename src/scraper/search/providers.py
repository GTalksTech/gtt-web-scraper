"""Search Providers: the external search services a query is sent to, one client each.

Each client speaks its Provider's own API (as documented in 2026-11) and turns the
answer into SearchResults, or raises ProviderError saying why it couldn't. A Provider
whose key isn't set refuses without calling anything. Firecrawl is never one of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from curl_cffi import requests as curl
from curl_cffi.requests.exceptions import RequestException
from ddgs.engines.duckduckgo import Duckduckgo
from ddgs.exceptions import DDGSException

from ..config import SearchConfig
# The same reason a Fetcher gives, so one Incident kind covers both.
from ..fetchers import QUOTA_EXHAUSTED


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    # The Search Provider that found it.
    provider: str
    # The Published date. As stored in the search cache, the Provider's own words
    # ("Oct 14, 2026", "3 days ago"); as shown to Callers, YYYY-MM-DD. None if unknown.
    published: str | None = None


# Why a Provider gave nothing, as its success rate records it. A Provider whose quota
# is used up, or whose key isn't set, is unavailable rather than failing.
PROVIDER_ERROR = "provider_error"
NOT_CONFIGURED = "not_configured"


class ProviderError(Exception):
    """The Provider gave no results to use. The message is for the Caller."""

    def __init__(self, message: str, reason: str = PROVIDER_ERROR) -> None:
        super().__init__(message)
        self.reason = reason


class SearchProvider(Protocol):
    name: str

    def search(self, query: str, limit: int) -> list[SearchResult]: ...


class _HttpProvider:
    """A Provider reached over its JSON API with a key."""

    name: str
    key_env: str
    # Responses that mean the Provider's free allowance, credits or rate limit is used up.
    quota_statuses: tuple[int, ...]

    def __init__(self, base_url: str, api_key: str | None, timeout_s: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_s = timeout_s

    def _key(self) -> str:
        if not self._api_key:
            raise ProviderError(f"not configured: {self.key_env} is not set", NOT_CONFIGURED)
        return self._api_key

    def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response: Any = curl.request(
                method,  # type: ignore[arg-type]
                f"{self._base_url}{path}",
                timeout=self._timeout_s,
                **kwargs,
            )
        except RequestException as exc:
            raise ProviderError(f"unreachable: {exc}") from exc
        status = response.status_code
        if status in self.quota_statuses:
            raise ProviderError(
                f"quota exhausted or rate-limited (HTTP {status})", QUOTA_EXHAUSTED
            )
        if status != 200:
            raise ProviderError(f"answered HTTP {status}: {response.text[:200]}")
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError("answered with something other than JSON") from exc
        if not isinstance(body, dict):
            raise ProviderError("answered with unexpected JSON")
        return body


def _results(body: dict[str, Any]) -> list[dict[str, Any]]:
    results = body.get("results")
    if not isinstance(results, list):
        raise ProviderError("answered without a results list")
    return [r for r in results if isinstance(r, dict) and r.get("url")]


class TinyFish(_HttpProvider):
    """docs.tinyfish.ai/search-api/reference: GET ?query=… with X-API-Key. No result
    count parameter; one page is about ten results."""

    name = "tinyfish"
    key_env = "TINYFISH_API_KEY"
    quota_statuses = (402, 429)  # 402 no credits; 429 per-minute or daily allowance

    def search(self, query: str, limit: int) -> list[SearchResult]:
        body = self._call(
            "GET", "/", params={"query": query}, headers={"X-API-Key": self._key()}
        )
        return [
            SearchResult(
                title=str(r.get("title") or ""),
                url=str(r["url"]),
                snippet=str(r.get("snippet") or ""),
                provider=self.name,
                published=r.get("date") or None,
            )
            for r in _results(body)
        ][:limit]


class Exa(_HttpProvider):
    """docs.exa.ai/reference/search: POST /search with x-api-key. Highlights are the
    snippet."""

    name = "exa"
    key_env = "EXA_API_KEY"
    quota_statuses = (402, 429)  # 402 out of credits or over budget; 429 rate limit

    def search(self, query: str, limit: int) -> list[SearchResult]:
        body = self._call(
            "POST",
            "/search",
            headers={"x-api-key": self._key()},
            json={
                "query": query,
                "numResults": limit,
                "type": "auto",
                "contents": {"highlights": {"maxCharacters": 400}},
            },
        )
        found = []
        for r in _results(body):
            highlights = r.get("highlights") or []
            snippet = " … ".join(str(h) for h in highlights) or str(r.get("summary") or "")
            found.append(
                SearchResult(
                    title=str(r.get("title") or ""),
                    url=str(r["url"]),
                    snippet=snippet,
                    provider=self.name,
                    published=r.get("publishedDate") or None,
                )
            )
        return found[:limit]


class Tavily(_HttpProvider):
    """docs.tavily.com/documentation/api-reference/endpoint/search: POST /search with a
    Bearer key. A basic search costs one credit; at most 20 results."""

    name = "tavily"
    key_env = "TAVILY_API_KEY"
    # 432 plan limit; 433 pay-as-you-go limit; 429 rate limit
    quota_statuses = (429, 432, 433)

    def search(self, query: str, limit: int) -> list[SearchResult]:
        body = self._call(
            "POST",
            "/search",
            headers={"Authorization": f"Bearer {self._key()}"},
            json={
                "query": query,
                "max_results": min(limit, 20),
                "search_depth": "basic",
                "topic": "general",
            },
        )
        return [
            SearchResult(
                title=str(r.get("title") or ""),
                url=str(r["url"]),
                snippet=str(r.get("content") or ""),
                provider=self.name,
                published=r.get("published_date") or None,
            )
            for r in _results(body)
        ][:limit]


class _DuckDuckGoPage(Duckduckgo):
    """ddgs's DuckDuckGo client (it reads the HTML results page), but saying why a page
    didn't come back instead of returning nothing: DuckDuckGo answers 202 when it
    rate-limits."""

    def request(self, *args: Any, **kwargs: Any) -> Any:
        response = self.http_client.request(*args, **kwargs)
        if response.status_code in (202, 429):
            raise ProviderError(f"rate-limited (HTTP {response.status_code})")
        if response.status_code != 200:
            raise ProviderError(f"answered HTTP {response.status_code}")
        return response.text


class DuckDuckGo:
    """DuckDuckGo's HTML results page, read with the ddgs package. No key; about ten
    results a page."""

    name = "duckduckgo"

    def __init__(self, base_url: str, timeout_s: float) -> None:
        self._search_url = f"{base_url.rstrip('/')}/html/"
        self._timeout_s = timeout_s

    def search(self, query: str, limit: int) -> list[SearchResult]:
        client = _DuckDuckGoPage(timeout=max(1, round(self._timeout_s)))
        client.search_url = self._search_url
        try:
            found = client.search(query) or []
        except DDGSException as exc:
            raise ProviderError(f"unreachable: {exc}") from exc
        return [
            SearchResult(title=r.title, url=r.href, snippet=r.body, provider=self.name)
            for r in found
            if r.href
        ][:limit]


def build_providers(config: SearchConfig) -> list[SearchProvider]:
    """The Search Providers named in config, in order."""
    available: dict[str, SearchProvider] = {
        "tinyfish": TinyFish(config.tinyfish_url, config.tinyfish_api_key, config.timeout_s),
        "exa": Exa(config.exa_url, config.exa_api_key, config.timeout_s),
        "tavily": Tavily(config.tavily_url, config.tavily_api_key, config.timeout_s),
        "duckduckgo": DuckDuckGo(config.duckduckgo_url, config.timeout_s),
    }
    unknown = [name for name in config.providers if name not in available]
    if unknown:
        raise ValueError(
            f"unknown Search Provider(s) in config: {', '.join(unknown)}; "
            f"expected some of: {', '.join(available)}"
        )
    return [available[name] for name in config.providers]
