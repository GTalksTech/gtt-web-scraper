"""Local PDF extraction: a PDF response's text layer as markdown, with its title and
Published date from the document info.

pypdf is pure Python, so it installs on the arm64 Pi without native builds. It reads
the text layer only: a scanned, image-only PDF extracts to nothing, the Content Gate
rejects it, and the request escalates to the Paid Fetcher.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import datetime

from pypdf import PdfReader

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PdfText:
    markdown: str  # "" when the PDF has no text layer or can't be read
    title: str | None
    published: str | None  # YYYY-MM-DD, like trafilatura's dates for HTML


def is_pdf(content_type: str, body: bytes) -> bool:
    """Whether a response is a PDF, decided by the body: the PDF header within the first
    1024 bytes (where readers accept it). Servers mislabel both ways, sending PDFs as
    application/octet-stream and HTML error pages as application/pdf, so the content
    type only rules out bodies it says are HTML."""
    return b"%PDF-" in body[:1024] and "html" not in content_type.lower()


def extract(body: bytes) -> PdfText:
    """The PDF's text layer as markdown, one paragraph per page, plus its document info."""
    try:
        reader = PdfReader(io.BytesIO(body))
        pages = [page.extract_text() or "" for page in reader.pages]
    # A PDF from the wild can break the parser in many ways (pypdf's own errors, but
    # also KeyError, ValueError, ...). An unreadable PDF is just one with no text: the
    # Gate rejects it and the request escalates.
    except Exception as exc:  # noqa: BLE001 (any parser failure)
        log.warning("PDF extraction failed: %s", exc)
        return PdfText("", None, None)
    # Pages become paragraphs; blank lines inside a page's text are dropped so a page
    # stays one block of lines.
    markdown = "\n\n".join(
        "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
        for text in pages
        if text.strip()
    )
    title, published = _info(reader)
    return PdfText(markdown, title, published)


def _info(reader: PdfReader) -> tuple[str | None, str | None]:
    try:
        info = reader.metadata
        title = (" ".join(str(info.title or "").split()) or None) if info else None
    except Exception:  # noqa: BLE001 (malformed info dictionary)
        return None, None
    try:
        created: datetime | None = (info.creation_date or info.modification_date) if info else None
    except Exception:  # noqa: BLE001 (malformed date string)
        created = None
    return title, created.date().isoformat() if created else None
