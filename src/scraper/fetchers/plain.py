"""The plain-download Fetcher: one HTTP request that looks like Chrome on the wire.

curl_cffi reproduces Chrome's TLS and HTTP/2 fingerprint and default headers, so the
request presents as a normal browser (ADR 0001).
"""

from __future__ import annotations

from curl_cffi import requests as curl
from curl_cffi.requests.exceptions import RequestException

from ..config import PlainConfig
from ..page import FetchedPage, build_page
from . import FetchError


class PlainFetcher:
    name = "plain"

    def __init__(self, config: PlainConfig) -> None:
        self._config = config

    def fetch(self, url: str) -> FetchedPage:
        try:
            response = curl.get(
                url,
                impersonate=self._config.impersonate,  # type: ignore[arg-type]
                timeout=self._config.timeout_s,
                allow_redirects=True,
            )
        except RequestException as exc:
            raise FetchError(str(exc)) from exc
        return build_page(
            requested_url=url,
            final_url=str(response.url),
            status=response.status_code,
            headers=dict(response.headers),
            html=response.text,
        )
