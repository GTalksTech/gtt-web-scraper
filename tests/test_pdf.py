"""Seam 1: PDFs are extracted on the Pi; a rejected extraction escalates to the Paid Fetcher."""

from __future__ import annotations

from fastapi.testclient import TestClient

from .fakefirecrawl import FakeFirecrawl, FirecrawlPage
from .fakeweb import FakeResponse, FakeWeb, page
from .test_scrape import scrape


def pdf(name: str, content_type: str = "application/pdf") -> FakeResponse:
    """A PDF fixture (rebuilt by tests/fixtures/make_pdfs.py)."""
    return page(name, content_type=content_type)


def test_text_pdf_comes_back_as_markdown_extracted_locally(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    url = web.serve("/docs/gtt-7.pdf", pdf("datasheet.pdf"))

    status, body = scrape(client, url)

    assert status == 200
    data = body["data"]
    assert "GTT-7 measures temperature, relative humidity" in data["markdown"]
    assert "Order code GTT-7-TR ships on tape and reel" in data["markdown"]
    assert data["metadata"]["title"] == "GTT-7 Environmental Sensor Datasheet"
    assert data["metadata"]["statusCode"] == 200
    assert data["publishedDate"] == "2026-10-02"
    assert data["fetcher"] == "plain"
    assert data["verdict"] == "accepted"
    assert firecrawl.scrapes == []


def test_pdf_is_detected_by_its_content_when_the_server_mislabels_it(
    client: TestClient, web: FakeWeb
) -> None:
    url = web.serve("/download?id=7", pdf("datasheet.pdf", "application/octet-stream"))

    status, body = scrape(client, url)

    assert status == 200
    assert "GTT-7 measures temperature" in body["data"]["markdown"]
    assert body["data"]["fetcher"] == "plain"


def test_html_page_mislabelled_as_pdf_is_still_read_as_html(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    url = web.serve("/report.pdf", page("article.html", content_type="application/pdf"))

    status, body = scrape(client, url)

    assert status == 200
    assert "Raspberry Pi 5 runs the" in body["data"]["markdown"]
    assert body["data"]["fetcher"] == "plain"
    assert firecrawl.scrapes == []


def test_image_only_pdf_is_rejected_locally_and_escalates_to_the_paid_fetcher(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    url = web.serve("/invoices/0042.pdf", pdf("scanned.pdf"))
    ocr_text = "Invoice 0042. " + "Two GTT-7 sensors on tape and reel, shipped 3 October. " * 25
    firecrawl.serve(url, FirecrawlPage(markdown=ocr_text, raw_html="", title="Scanned invoice"))

    status, body = scrape(client, url)

    assert status == 200
    data = body["data"]
    assert data["fetcher"] == "paid"
    assert data["markdown"].startswith("Invoice 0042.")
    plain, paid = data["attempts"]
    assert (plain["fetcher"], plain["verdict"], plain["reason"]) == ("plain", "rejected", "too_short")
    assert (paid["fetcher"], paid["verdict"]) == ("paid", "accepted")
    assert [s.body["url"] for s in firecrawl.scrapes] == [url]


def test_unreadable_pdf_is_rejected_and_escalates_instead_of_erroring(
    client: TestClient, web: FakeWeb, firecrawl: FakeFirecrawl
) -> None:
    broken = FakeResponse(b"%PDF-1.7\nnot really a pdf", content_type="application/pdf")
    url = web.serve("/broken.pdf", broken)

    status, body = scrape(client, url)

    assert status == 502
    plain, paid = body["attempts"]
    assert (plain["fetcher"], plain["verdict"], plain["reason"]) == ("plain", "rejected", "too_short")
    assert paid["fetcher"] == "paid"
    assert len(firecrawl.scrapes) == 1
