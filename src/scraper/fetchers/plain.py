"""The plain-download Fetcher: one HTTP request that looks like Chrome on the wire.

curl_cffi reproduces Chrome's TLS and HTTP/2 fingerprint and default headers, so the
request presents as a normal browser (ADR 0001).

A response that is a PDF (by its %PDF- header; see pdf.is_pdf) is
extracted locally instead of as HTML.
"""

from __future__ import annotations

from curl_cffi import requests as curl
from curl_cffi.requests.exceptions import RequestException

from ..config import PlainConfig
from ..page import FetchedPage, build_page, build_pdf_page
from ..pdf import is_pdf
from . import FetchError


class PlainFetcher:
    name = "plain"
    polls_challenges = True

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
        headers = dict(response.headers)
        final_url = str(response.url)
        content_type = response.headers.get("content-type") or ""
        if is_pdf(content_type, response.content):
            return build_pdf_page(url, final_url, response.status_code, headers, response.content)
        return build_page(
            requested_url=url,
            final_url=final_url,
            status=response.status_code,
            headers=headers,
            html=response.text,
        )
