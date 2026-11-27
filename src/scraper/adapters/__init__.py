"""Site Adapters: routes that catch URLs from one site and get the content through that
site's official feed or API instead of the Ladder.

An adapter's result is judged by the Content Gate like any Fetcher's. If the adapter
fails (FetchError) or its result is rejected, the request falls back to the Ladder.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from curl_cffi import requests as curl
from curl_cffi.requests.exceptions import RequestException

from ..config import AdaptersConfig
from ..fetchers import FetchError
from ..page import FetchedPage, page_with_markdown

# Sent on every adapter request: the APIs ask clients to identify themselves.
USER_AGENT = "gtt-web-scraper/0.1 (self-hosted page reader)"


# Every Site Adapter's name. None of them is also a Fetcher's.
ADAPTER_NAMES = frozenset({"youtube", "reddit", "github"})


class ApiError(FetchError):
    """A feed or API answered, but with an error status instead of content."""

    def __init__(self, what: str, status: int) -> None:
        super().__init__(f"{what} answered {status}")
        self.status = status


class SiteAdapter(Protocol):
    name: str
    # Whether the adapter reaches the requested URL's site over the home connection.
    # Those adapters are spaced per site, skip a site on a cool-down, and start a
    # cool-down when the site answers 403 or 429.
    home_connection: bool

    def handles(self, url: str) -> bool:
        """Whether this adapter knows how to get `url` from its site's feed or API."""
        ...

    def fetch(self, url: str) -> FetchedPage:
        """The content of `url`, from the feed or API. Raises FetchError if it can't."""
        ...


def build_adapters(config: AdaptersConfig) -> list[SiteAdapter]:
    from .github import GitHubAdapter
    from .reddit import RedditAdapter
    from .youtube import YouTubeAdapter

    available: dict[str, SiteAdapter] = {
        "youtube": YouTubeAdapter(config),
        "reddit": RedditAdapter(config),
        "github": GitHubAdapter(config),
    }
    return [available[name] for name in config.enabled]


def api_get(
    url: str, what: str, timeout_s: float, headers: dict[str, str] | None = None
) -> Any:
    """One GET to a feed or API; anything but a 200 is a FetchError naming `what`."""
    try:
        response = curl.get(
            url,
            headers={"User-Agent": USER_AGENT, **(headers or {})},
            timeout=timeout_s,
            allow_redirects=True,
        )
    except RequestException as exc:
        raise FetchError(f"{what} unreachable: {exc}") from exc
    if response.status_code != 200:
        raise ApiError(what, response.status_code)
    return response


def api_get_json(
    url: str, what: str, timeout_s: float, headers: dict[str, str] | None = None
) -> Any:
    response = api_get(url, what, timeout_s, headers)
    try:
        return response.json()
    except ValueError as exc:
        raise FetchError(f"{what} answered with something other than JSON") from exc


def adapter_page(
    url: str, markdown: str, title: str | None, published: str | None
) -> FetchedPage:
    """A page built from feed or API data: no HTML, markdown written by the adapter."""
    return page_with_markdown(
        requested_url=url,
        final_url=url,
        status=200,
        headers={},
        html="",
        markdown=markdown,
        title=title,
        published=published,
    )


def iso_date(value: object) -> str | None:
    """The date part of an ISO 8601 timestamp ("2026-11-20T10:00:00Z" → "2026-11-20")."""
    return value[:10] if isinstance(value, str) and len(value) >= 10 else None


def epoch_date(value: object) -> str | None:
    """The UTC date of a Unix timestamp."""
    if not isinstance(value, int | float):
        return None
    return datetime.fromtimestamp(value, UTC).date().isoformat()
