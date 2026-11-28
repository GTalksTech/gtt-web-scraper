"""Seam 1: the search chain, driven through the Firecrawl-compatible search endpoints
against fake Search Providers (and a fake Firecrawl that search must never touch)."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from scraper.api import create_app
from scraper.config import Config, SourceList, load_config

from .fakefirecrawl import FakeFirecrawl
from .fakesearch import PROVIDERS, FakeSearchProviders, Hit, hits


def search_v2(client: TestClient, query: str, **extra: Any) -> tuple[int, dict[str, Any]]:
    response = client.post("/v2/search", json={"query": query, **extra})
    return response.status_code, response.json()


def test_v2_search_returns_the_first_providers_results_in_firecrawls_shape(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    status, body = search_v2(client, "raspberry pi 5 thermals")

    assert status == 200
    assert body["success"] is True
    web = body["data"]["web"]
    assert [r["url"] for r in web] == [
        "https://example.org/tinyfish/1",
        "https://example.org/tinyfish/2",
        "https://example.org/tinyfish/3",
    ]
    first = web[0]
    assert first["title"] == "tinyfish result 1"
    assert first["description"] == "What tinyfish says about result 1."
    assert first["provider"] == "tinyfish"
    assert providers.queries["tinyfish"] == ["raspberry pi 5 thermals"]
    assert providers.calls() == ["tinyfish"]


def test_a_failing_provider_passes_the_query_to_the_next(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    providers.fail("tinyfish")

    status, body = search_v2(client, "pi 5 nvme boot")

    assert status == 200
    assert [r["provider"] for r in body["data"]["web"]] == ["exa", "exa", "exa"]
    assert body["data"]["web"][0]["description"] == "What exa says about result 1."
    assert body["provider"] == "exa"
    assert body["attempts"] == [
        {"provider": "tinyfish", "error": body["attempts"][0]["error"], "reason": "provider_error"}
    ]
    assert "HTTP 500" in body["attempts"][0]["error"]
    assert providers.calls() == ["tinyfish", "exa"]


def test_out_of_quota_providers_pass_the_query_down_the_chain_in_order(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    providers.run_out("tinyfish")
    providers.run_out("exa")

    status, body = search_v2(client, "pi 5 nvme boot")

    assert status == 200
    assert {r["provider"] for r in body["data"]["web"]} == {"tavily"}
    assert body["data"]["web"][1]["description"] == "What tavily says about result 2."
    assert [a["provider"] for a in body["attempts"]] == ["tinyfish", "exa"]
    assert all("quota" in a["error"] for a in body["attempts"])
    assert providers.calls() == ["tinyfish", "exa", "tavily"]


def test_duckduckgo_answers_when_every_keyed_provider_is_down(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    for name in ("tinyfish", "exa", "tavily"):
        providers.fail(name)

    status, body = search_v2(client, "pi 5 nvme boot")

    assert status == 200
    web = body["data"]["web"]
    assert [r["url"] for r in web] == [
        "https://example.org/duckduckgo/1",
        "https://example.org/duckduckgo/2",
        "https://example.org/duckduckgo/3",
    ]
    assert web[0]["title"] == "duckduckgo result 1"
    assert web[0]["description"] == "What duckduckgo says about result 1."
    assert providers.queries["duckduckgo"] == ["pi 5 nvme boot"]


def test_provider_order_comes_from_config(
    config: Config, providers: FakeSearchProviders
) -> None:
    config.search.providers = ["tavily", "tinyfish"]
    client = TestClient(create_app(config))

    status, body = search_v2(client, "pi 5 nvme boot")

    assert status == 200
    assert body["provider"] == "tavily"
    assert providers.calls() == ["tavily"]


def test_a_provider_without_its_key_is_skipped_without_being_called(
    config: Config, providers: FakeSearchProviders
) -> None:
    config.search.tinyfish_api_key = None
    client = TestClient(create_app(config))

    status, body = search_v2(client, "pi 5 nvme boot")

    assert status == 200
    assert body["provider"] == "exa"
    assert body["attempts"] == [
        {
            "provider": "tinyfish",
            "error": "not configured: TINYFISH_API_KEY is not set",
            "reason": "not_configured",
        }
    ]
    assert providers.calls() == ["exa"]


def test_a_provider_that_finds_nothing_passes_the_query_on(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    providers.answer("tinyfish", [])

    status, body = search_v2(client, "an obscure query")

    assert status == 200
    assert body["provider"] == "exa"
    assert body["attempts"] == [
        {"provider": "tinyfish", "error": "found no results", "reason": "no_results"}
    ]


def test_every_provider_failing_is_an_explicit_failure_naming_each_error(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    providers.fail("tinyfish")
    providers.run_out("exa")
    providers.run_out("tavily")
    providers.run_out("duckduckgo")
    providers.run_out("searxng")

    status, body = search_v2(client, "pi 5 nvme boot")

    assert status == 502
    assert body["success"] is False
    assert body["code"] == "SEARCH_FAILED"
    assert "data" not in body
    assert [a["provider"] for a in body["attempts"]] == [
        "tinyfish",
        "exa",
        "tavily",
        "duckduckgo",
        "searxng",
    ]
    error = body["error"]
    assert "Every Search Provider failed" in error
    assert "tinyfish: answered HTTP 500" in error
    assert "exa: quota exhausted or rate-limited (HTTP 402)" in error
    assert "tavily: quota exhausted or rate-limited (HTTP 432)" in error
    assert "duckduckgo: rate-limited (HTTP 202)" in error
    assert "searxng: every upstream search service failed: bing (Suspended: too many requests)" in error


def test_results_are_cached_for_24_hours(
    config: Config, providers: FakeSearchProviders
) -> None:
    now = [1_800_000_000.0]
    client = TestClient(create_app(config, clock=lambda: now[0]))

    _, first = search_v2(client, "pi 5 nvme boot")
    providers.fail("tinyfish")
    now[0] += 23 * 3600
    status, again = search_v2(client, "  Pi 5   NVMe boot ")

    assert status == 200
    assert first["cacheState"] == "miss"
    assert first["cachedAt"] is None
    assert again["cacheState"] == "hit"
    assert again["cachedAt"] == "2027-01-15T08:00:00Z"
    assert again["data"] == first["data"]
    assert again["provider"] == "tinyfish"
    assert providers.queries["tinyfish"] == ["pi 5 nvme boot"]

    now[0] += 2 * 3600  # 25 hours after the first search
    _, later = search_v2(client, "pi 5 nvme boot")

    assert later["cacheState"] == "miss"
    assert later["provider"] == "exa"


def test_a_cached_search_serves_smaller_limits_and_a_larger_one_searches_again(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    providers.answer("tinyfish", hits("tinyfish", 12))

    search_v2(client, "pi 5 nvme boot")  # limit 10
    v1 = client.post("/v1/search", json={"query": "pi 5 nvme boot"}).json()  # limit 5

    assert v1["cacheState"] == "hit"
    assert len(v1["data"]) == 5
    assert providers.queries["tinyfish"] == ["pi 5 nvme boot"]

    _, more = search_v2(client, "pi 5 nvme boot", limit=12)

    assert more["cacheState"] == "miss"
    assert len(more["data"]["web"]) == 12
    assert len(providers.queries["tinyfish"]) == 2


def test_a_failed_search_is_not_cached(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    for name in PROVIDERS:
        providers.fail(name)
    search_v2(client, "pi 5 nvme boot")
    providers.answer("tinyfish", hits("tinyfish"))

    status, body = search_v2(client, "pi 5 nvme boot")

    assert status == 200
    assert body["cacheState"] == "miss"
    assert body["provider"] == "tinyfish"


def test_limit_caps_the_results_and_reaches_the_provider(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    providers.fail("tinyfish")
    providers.answer("exa", hits("exa", 8))

    status, body = search_v2(client, "pi 5 nvme boot", limit=2)

    assert status == 200
    assert len(body["data"]["web"]) == 2
    assert providers.requests["exa"][0]["numResults"] == 2


def test_limit_defaults_to_firecrawls_default(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    providers.answer("tinyfish", hits("tinyfish", 12))

    _, v2 = search_v2(client, "pi 5 nvme boot")
    v1 = client.post("/v1/search", json={"query": "pi 5 nvme boot"}).json()

    assert len(v2["data"]["web"]) == 10
    assert len(v1["data"]) == 5


def test_v1_search_returns_results_as_a_list(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    providers.answer(
        "tinyfish",
        [Hit("Pi 5 review", "https://example.org/review", "A long review.", "2026-10-14")],
    )

    response = client.post(
        "/v1/search", json={"query": "pi 5 review", "limit": 3, "scrapeOptions": {}}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"] == [
        {
            "title": "Pi 5 review",
            "description": "A long review.",
            "url": "https://example.org/review",
            "snippet": "A long review.",
            "provider": "tinyfish",
            "publishedDate": "2026-10-14",
        }
    ]


def test_search_never_calls_firecrawl(
    client: TestClient, providers: FakeSearchProviders, firecrawl: FakeFirecrawl
) -> None:
    search_v2(client, "pi 5 nvme boot")
    for name in ("tinyfish", "exa", "tavily", "duckduckgo"):
        providers.fail(name)
    search_v2(client, "something else")
    client.post("/v1/search", json={"query": "a third query"})

    assert firecrawl.paths == []


def test_an_empty_query_is_refused(client: TestClient, providers: FakeSearchProviders) -> None:
    response = client.post("/v2/search", json={"query": ""})

    assert response.status_code == 422
    assert providers.calls() == []


# ── SearXNG: the self-hosted last resort ───────────────────────────────────

FREE_TIER = ("tinyfish", "exa", "tavily", "duckduckgo")


def test_searxng_is_last_in_the_configured_order() -> None:
    assert load_config().search.providers[-1] == "searxng"


def test_searxng_answers_when_every_other_provider_has_failed(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    providers.fail("tinyfish")
    providers.run_out("exa")
    providers.run_out("tavily")
    providers.fail("duckduckgo")

    status, body = search_v2(client, "pi 5 nvme boot")

    assert status == 200
    assert body["provider"] == "searxng"
    web = body["data"]["web"]
    assert [r["url"] for r in web] == [
        "https://example.org/searxng/1",
        "https://example.org/searxng/2",
        "https://example.org/searxng/3",
    ]
    assert web[0]["title"] == "searxng result 1"
    assert web[0]["description"] == "What searxng says about result 1."
    assert web[0]["provider"] == "searxng"
    assert [a["provider"] for a in body["attempts"]] == list(FREE_TIER)
    assert providers.calls() == [*FREE_TIER, "searxng"]
    # Asked for JSON, the only answer the service reads; no key is sent.
    assert providers.requests["searxng"] == [{"q": "pi 5 nvme boot", "format": "json"}]


def test_searxng_is_not_asked_while_an_earlier_provider_answers(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    for name in ("tinyfish", "exa", "tavily"):
        providers.fail(name)

    status, body = search_v2(client, "pi 5 nvme boot")

    assert status == 200
    assert body["provider"] == "duckduckgo"
    assert providers.queries["searxng"] == []


def test_searxng_failing_too_is_an_explicit_failure_naming_it(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    for name in PROVIDERS:
        providers.fail(name)

    status, body = search_v2(client, "pi 5 nvme boot")

    assert status == 502
    assert body["code"] == "SEARCH_FAILED"
    assert [a["provider"] for a in body["attempts"]] == [*FREE_TIER, "searxng"]
    assert body["attempts"][-1]["reason"] == "provider_error"
    assert "searxng: answered HTTP 500" in body["error"]


def test_searxng_whose_upstream_services_all_block_it_names_each(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    for name in FREE_TIER:
        providers.fail(name)
    providers.run_out("searxng")

    status, body = search_v2(client, "pi 5 nvme boot")

    assert status == 502
    assert body["attempts"][-1] == {
        "provider": "searxng",
        "error": "every upstream search service failed: "
        "bing (Suspended: too many requests), brave (timeout), google (Suspended: CAPTCHA)",
        "reason": "provider_error",
    }


def test_searxng_results_without_a_usable_url_are_dropped(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    # Seen from the real SearXNG: an upstream service's redirect link it couldn't resolve.
    for name in FREE_TIER:
        providers.fail(name)
    providers.answer(
        "searxng",
        [
            Hit("Broken", "http:///go/v1/AV3PJ5vjNPk3", "A redirect."),
            Hit("Relative", "/url?q=x", "Relative."),
            Hit("Docs", "https://git-scm.com/docs/git-rebase", "Docs."),
        ],
    )

    status, body = search_v2(client, "git rebase documentation")

    assert status == 200
    assert [r["url"] for r in body["data"]["web"]] == ["https://git-scm.com/docs/git-rebase"]


def test_searxng_finding_nothing_is_no_results(
    client: TestClient, providers: FakeSearchProviders
) -> None:
    for name in FREE_TIER:
        providers.fail(name)
    providers.answer("searxng", [])

    status, body = search_v2(client, "an obscure query")

    assert status == 502
    assert body["attempts"][-1] == {
        "provider": "searxng",
        "error": "found no results",
        "reason": "no_results",
    }


def test_searxng_unreachable_is_a_failure(
    config: Config, providers: FakeSearchProviders
) -> None:
    # Its container is down: nothing listens where the scraper expects it.
    config.search.searxng_url = "http://127.0.0.1:9"
    client = TestClient(create_app(config))
    for name in FREE_TIER:
        providers.fail(name)

    status, body = search_v2(client, "pi 5 nvme boot")

    assert status == 502
    assert body["attempts"][-1]["provider"] == "searxng"
    assert body["attempts"][-1]["error"].startswith("unreachable")


def test_searxng_results_are_limited_ranked_and_cached(
    config: Config, providers: FakeSearchProviders
) -> None:
    config.source_list = SourceList(trusted=["python.org"], blocked=["contentfarm.example"])
    client = TestClient(create_app(config))
    for name in FREE_TIER:
        providers.fail(name)
    providers.answer(
        "searxng",
        [
            Hit("Farm", "https://contentfarm.example/pi", "Spam."),
            Hit("Other", "https://example.org/pi", "Fine."),
            Hit("Docs", "https://docs.python.org/3/", "Docs."),
            *hits("searxng", 20),
        ],
    )

    _, first = search_v2(client, "pi 5 nvme boot", limit=3)
    _, again = search_v2(client, "pi 5 nvme boot", limit=3)

    # Its first three, trusted first and the blocked one dropped, as for any Provider.
    assert [r["url"] for r in first["data"]["web"]] == [
        "https://docs.python.org/3/",
        "https://example.org/pi",
    ]
    assert again["cacheState"] == "hit"
    assert again["provider"] == "searxng"
    assert again["data"] == first["data"]
    assert len(providers.queries["searxng"]) == 1


def test_searxng_can_be_forced(client: TestClient, providers: FakeSearchProviders) -> None:
    status, body = search_v2(client, "pi 5 nvme boot", provider="searxng")

    assert status == 200
    assert body["provider"] == "searxng"
    assert providers.calls() == ["searxng"]
