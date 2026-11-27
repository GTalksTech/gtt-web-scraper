"""Seam 1: the Source List shaping search results (trusted domains boosted, blocked
domains dropped), driven through the search endpoints against fake Search Providers."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scraper.api import create_app
from scraper.config import Config, SourceList, load_config

from .fakesearch import FakeSearchProviders, Hit
from .test_search import search_v2


def hit(url: str) -> Hit:
    return Hit(title=f"About {url}", url=url, snippet="Some text.")


def urls(body: dict) -> list[str]:  # type: ignore[type-arg]
    return [r["url"] for r in body["data"]["web"]]


@pytest.fixture
def ranked(config: Config) -> TestClient:
    config.source_list = SourceList(
        trusted=["python.org", "github.com"], blocked=["contentfarm.example", "spam.github.com"]
    )
    return TestClient(create_app(config))


def test_trusted_domains_are_boosted_keeping_the_providers_order_otherwise(
    ranked: TestClient, providers: FakeSearchProviders
) -> None:
    providers.answer(
        "tinyfish",
        [
            hit("https://example.org/a"),
            hit("https://docs.python.org/3/library/asyncio.html"),
            hit("https://example.org/b"),
            hit("https://github.com/python/cpython"),
        ],
    )

    status, body = search_v2(ranked, "asyncio")

    assert status == 200
    assert urls(body) == [
        "https://docs.python.org/3/library/asyncio.html",
        "https://github.com/python/cpython",
        "https://example.org/a",
        "https://example.org/b",
    ]


def test_blocked_domains_and_their_subdomains_are_dropped(
    ranked: TestClient, providers: FakeSearchProviders
) -> None:
    providers.answer(
        "tinyfish",
        [
            hit("https://contentfarm.example/10-best-routers"),
            hit("https://example.org/a"),
            hit("https://www.contentfarm.example/asyncio"),
            hit("https://spam.github.com/x"),
            hit("https://github.com/python/cpython"),
        ],
    )

    _, body = search_v2(ranked, "asyncio")

    # A blocked subdomain of a trusted domain is still blocked.
    assert urls(body) == ["https://github.com/python/cpython", "https://example.org/a"]


def test_a_domain_that_only_looks_like_a_listed_one_is_not_matched(
    ranked: TestClient, providers: FakeSearchProviders
) -> None:
    providers.answer(
        "tinyfish",
        [
            hit("https://example.org/a"),
            hit("https://notgithub.com/x"),
            hit("https://github.com.evil.example/y"),
        ],
    )

    _, body = search_v2(ranked, "asyncio")

    assert urls(body) == [
        "https://example.org/a",
        "https://notgithub.com/x",
        "https://github.com.evil.example/y",
    ]


def test_a_provider_whose_results_are_all_blocked_passes_the_query_on(
    ranked: TestClient, providers: FakeSearchProviders
) -> None:
    providers.answer("tinyfish", [hit("https://contentfarm.example/a")])

    status, body = search_v2(ranked, "asyncio")

    assert status == 200
    assert body["provider"] == "exa"
    assert body["attempts"] == [
        {"provider": "tinyfish", "error": "found only results from blocked domains"}
    ]


def test_the_source_list_also_shapes_cached_searches(
    config: Config, providers: FakeSearchProviders
) -> None:
    providers.answer(
        "tinyfish", [hit("https://example.org/a"), hit("https://github.com/python/cpython")]
    )
    search_v2(TestClient(create_app(config)), "asyncio")  # cached unranked by an empty list
    config.source_list = SourceList(trusted=["github.com"], blocked=["example.org"])

    _, body = search_v2(TestClient(create_app(config)), "asyncio")

    assert body["cacheState"] == "hit"
    assert urls(body) == ["https://github.com/python/cpython"]


def test_mcp_search_is_ranked_the_same_way(
    config: Config, providers: FakeSearchProviders
) -> None:
    from .mcpcaller import McpCaller
    from .test_mcp_fetch import data

    config.source_list = SourceList(trusted=["github.com"], blocked=["contentfarm.example"])
    providers.answer(
        "tinyfish",
        [
            hit("https://example.org/a"),
            hit("https://contentfarm.example/b"),
            hit("https://github.com/python/cpython"),
        ],
    )
    caller = McpCaller(create_app(config))
    caller.start()
    try:
        result = caller.call("search", query="asyncio")
    finally:
        caller.stop()

    assert [r["url"] for r in data(result)["results"]] == [
        "https://github.com/python/cpython",
        "https://example.org/a",
    ]


# ── the seeded Source List ─────────────────────────────────────────────────────


def test_the_source_list_is_seeded_from_its_own_config_file() -> None:
    source_list = load_config().source_list

    for domain in ("ietf.org", "github.com", "cisco.com", "anthropic.com", "arxiv.org"):
        assert domain in source_list.trusted
    assert "pinterest.com" in source_list.blocked
    assert not set(source_list.trusted) & set(source_list.blocked)


def write_config(tmp_path: Path, source_list: str) -> Path:
    (tmp_path / "scraper.yaml").write_text("search:\n  source_list: sources.yaml\n")
    (tmp_path / "sources.yaml").write_text(source_list)
    return tmp_path / "scraper.yaml"


def test_source_list_entries_are_normalised_domains(tmp_path: Path) -> None:
    path = write_config(tmp_path, "trusted:\n  - GitHub.com\n  - www.python.org.\nblocked: []\n")

    assert load_config(path).source_list.trusted == ["github.com", "python.org"]


@pytest.mark.parametrize(
    "bad",
    [
        "trusted:\n  - https://github.com/python\nblocked: []\n",
        "trusted:\n  - github.com\nblocked:\n  - github.com\n",
        "trusted:\n  - ''\nblocked: []\n",
    ],
)
def test_a_malformed_source_list_is_refused_at_startup(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError, match="Source List"):
        load_config(write_config(tmp_path, bad))
