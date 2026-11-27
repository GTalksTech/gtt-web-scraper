"""Seam 2: every page in the Content Gate corpus gets its expected Verdict."""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import Any

import pytest
import yaml

from scraper.config import load_config
from scraper.gate import judge
from scraper.page import build_page

CORPUS = Path(__file__).parent / "corpus"
ENTRIES: list[dict[str, Any]] = yaml.safe_load((CORPUS / "corpus.yaml").read_text())


def test_every_corpus_entry_has_its_page() -> None:
    pages = {p.name.removesuffix(".html.gz") for p in (CORPUS / "pages").glob("*.html.gz")}
    assert pages == {e["name"] for e in ENTRIES}


@pytest.mark.parametrize("entry", ENTRIES, ids=[e["name"] for e in ENTRIES])
def test_content_gate_verdict(entry: dict[str, Any]) -> None:
    raw = gzip.decompress((CORPUS / "pages" / f"{entry['name']}.html.gz").read_bytes())
    page = build_page(
        requested_url=entry["url"],
        final_url=entry["url"],
        status=entry["status"],
        headers=entry.get("headers", {}),
        html=raw.decode("utf-8", errors="replace"),
    )

    verdict = judge(page, load_config().gate)

    assert verdict.decision == entry["expected"], verdict
    if "reason" in entry:
        assert verdict.reason == entry["reason"], verdict
