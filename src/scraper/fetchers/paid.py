"""The Paid Fetcher: Firecrawl's scrape endpoint, the last Fetcher on the Ladder.

Firecrawl answers HTTP 402 when the account is out of credits. From then on the Paid
Fetcher is unavailable: it refuses without calling Firecrawl, so requests that need it
fail explicitly, until `quota_recheck_s` has passed and one request checks again.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from curl_cffi import requests as curl
from curl_cffi.requests.exceptions import RequestException

from ..config import PaidConfig
from ..gate import NOT_CONFIGURED, QUOTA_EXHAUSTED
from ..page import FetchedPage, page_with_markdown
from . import FetchError, FetcherUnavailable


class PaidFetcher:
    name = "paid"
    polls_challenges = False  # every fetch spends a credit

    def __init__(self, config: PaidConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._exhausted_at: float | None = None

    def fetch(self, url: str) -> FetchedPage:
        if not self._config.api_key:
            raise FetcherUnavailable(
                NOT_CONFIGURED, "Paid Fetcher unavailable: FIRECRAWL_API_KEY is not set"
            )
        if self._quota_exhausted():
            raise _quota_exhausted_error()
        try:
            response = curl.post(
                f"{self._config.base_url.rstrip('/')}/v2/scrape",
                headers={"Authorization": f"Bearer {self._config.api_key}"},
                json={"url": url, "formats": ["markdown", "rawHtml"], "onlyMainContent": True},
                timeout=self._config.timeout_s,
            )
        except RequestException as exc:
            raise FetchError(f"Firecrawl unreachable: {exc}") from exc
        if response.status_code == 402:
            with self._lock:
                self._exhausted_at = time.monotonic()
            raise _quota_exhausted_error()
        body = _json(response)
        if response.status_code != 200 or not body.get("success"):
            error = body.get("error") or response.text[:200]
            raise FetchError(f"Firecrawl answered {response.status_code}: {error}")
        with self._lock:
            self._exhausted_at = None
        return _page(url, body.get("data") or {})

    def _quota_exhausted(self) -> bool:
        with self._lock:
            if self._exhausted_at is None:
                return False
            if time.monotonic() - self._exhausted_at < self._config.quota_recheck_s:
                return True
            # Time to check again: let this request through, and keep the others
            # refused until it answers.
            self._exhausted_at = time.monotonic()
            return False


def _quota_exhausted_error() -> FetcherUnavailable:
    return FetcherUnavailable(QUOTA_EXHAUSTED, "Paid Fetcher unavailable: quota exhausted")


def _json(response: Any) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _page(url: str, data: dict[str, Any]) -> FetchedPage:
    metadata: dict[str, Any] = data.get("metadata") or {}
    return page_with_markdown(
        requested_url=url,
        final_url=str(metadata.get("url") or metadata.get("sourceURL") or url),
        status=int(metadata.get("statusCode") or 200),
        headers={},  # Firecrawl doesn't pass on the site's response headers
        html=str(data.get("rawHtml") or ""),
        markdown=str(data.get("markdown") or ""),
        title=metadata.get("title") or None,
        published=metadata.get("publishedTime") or None,
    )
