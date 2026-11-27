"""A fetched page: the raw response plus its extracted main content."""

from __future__ import annotations

from dataclasses import dataclass

import lxml.html
import trafilatura
from lxml import etree


@dataclass(frozen=True)
class FetchedPage:
    requested_url: str
    final_url: str
    status: int
    headers: dict[str, str]  # lower-cased names
    html: str
    title: str | None
    markdown: str  # extracted main content; "" when nothing was found
    published: str | None  # Published date, when the page's metadata gives one


def build_page(
    requested_url: str, final_url: str, status: int, headers: dict[str, str], html: str
) -> FetchedPage:
    """Extract the main content of an HTML response as markdown."""
    markdown = (
        trafilatura.extract(
            html,
            url=final_url,
            output_format="markdown",
            include_links=True,
            include_formatting=True,
            include_comments=False,
            favor_recall=True,
        )
        or ""
    )
    meta = trafilatura.extract_metadata(html, default_url=final_url)
    return FetchedPage(
        requested_url=requested_url,
        final_url=final_url,
        status=status,
        headers={k.lower(): v for k, v in headers.items()},
        html=html,
        # The raw <title>, as Firecrawl reports it; the Gate reads it too.
        title=_title_tag(html) or (meta.title if meta and meta.title else None),
        markdown=markdown.strip(),
        published=(meta.date if meta and meta.date else None),
    )


def _title_tag(html: str) -> str | None:
    if not html.strip():
        return None
    try:
        tree = lxml.html.fromstring(html)
    except (etree.ParserError, ValueError):
        return None
    title = tree.findtext(".//title")
    return " ".join(title.split()) if title and title.strip() else None
