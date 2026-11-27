"""A fetched page: the raw response plus its extracted main content."""

from __future__ import annotations

from dataclasses import dataclass

import lxml.html
import trafilatura
from lxml import etree

from . import pdf


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
    return page_with_markdown(requested_url, final_url, status, headers, html, markdown)


def build_pdf_page(
    requested_url: str, final_url: str, status: int, headers: dict[str, str], body: bytes
) -> FetchedPage:
    """Extract a PDF response's text as markdown. The page has no HTML; its title and
    Published date come from the PDF's document info."""
    text = pdf.extract(body)
    return page_with_markdown(
        requested_url,
        final_url,
        status,
        headers,
        html="",
        markdown=text.markdown,
        title=text.title,
        published=text.published,
    )


def page_with_markdown(
    requested_url: str,
    final_url: str,
    status: int,
    headers: dict[str, str],
    html: str,
    markdown: str,
    title: str | None = None,
    published: str | None = None,
) -> FetchedPage:
    """A page whose main content is already markdown (extracted here, or by a service
    like Firecrawl). Title and Published date come from the HTML when it has them."""
    meta = trafilatura.extract_metadata(html, default_url=final_url) if html.strip() else None
    return FetchedPage(
        requested_url=requested_url,
        final_url=final_url,
        status=status,
        headers={k.lower(): v for k, v in headers.items()},
        html=html,
        # The raw <title>, as Firecrawl reports it; the Gate reads it too.
        title=_title_tag(html) or (meta.title if meta and meta.title else None) or title,
        markdown=markdown.strip(),
        published=(meta.date if meta and meta.date else None) or published,
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
