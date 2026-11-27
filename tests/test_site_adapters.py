"""Seam 1: Site Adapters, driven through the Firecrawl-compatible scrape endpoint.

YouTube, Reddit and GitHub URLs go to each site's official feed or API (faked here)
instead of the Ladder. The Ladder in these tests is only the Paid Fetcher, talking to
the fake Firecrawl, so a fallback never reaches the real internet.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from scraper.api import create_app
from scraper.config import Config

from .fakefirecrawl import FakeFirecrawl, firecrawl_page
from .fakeweb import FakeResponse, FakeWeb
from .test_paid_fetcher import ARTICLE_MARKDOWN
from .test_scrape import scrape

GITHUB_TOKEN = "github_pat_test-token"

LOREM = (
    "This paragraph stands in for real prose so the Content Gate sees a page with "
    "enough text to be accepted. It repeats a few times to clear the threshold. "
)


@dataclass
class Apis:
    youtube: FakeWeb
    reddit: FakeWeb
    github: FakeWeb


@pytest.fixture
def apis() -> Iterator[Apis]:
    fakes = Apis(FakeWeb(), FakeWeb(), FakeWeb())
    for fake in (fakes.youtube, fakes.reddit, fakes.github):
        fake.start()
    yield fakes
    for fake in (fakes.youtube, fakes.reddit, fakes.github):
        fake.stop()


@pytest.fixture
def adapter_config(config: Config, apis: Apis) -> Config:
    config.adapters.youtube_url = apis.youtube.url("")
    config.adapters.reddit_url = apis.reddit.url("")
    config.adapters.github_api_url = apis.github.url("")
    config.adapters.github_token = GITHUB_TOKEN
    # Only the Paid Fetcher (the fake Firecrawl): a fallback never touches the internet.
    config.ladder = ["paid"]
    return config


@pytest.fixture
def client(adapter_config: Config) -> TestClient:
    return TestClient(create_app(adapter_config))


def as_json(body: Any) -> FakeResponse:
    return FakeResponse(json.dumps(body).encode(), content_type="application/json")


def text(body: str, content_type: str = "text/plain; charset=utf-8") -> FakeResponse:
    return FakeResponse(body.encode(), content_type=content_type)


# ── GitHub ────────────────────────────────────────────────────────────────

REPO = {
    "full_name": "octo/widget",
    "description": "A small widget library for tests",
    "stargazers_count": 1234,
    "forks_count": 56,
    "language": "Python",
    "license": {"spdx_id": "MIT"},
    "default_branch": "main",
    "pushed_at": "2026-11-20T10:00:00Z",
    "topics": ["widgets", "testing"],
    "homepage": "",
}
README = "# Widget\n\n" + LOREM * 12


def serve_repo(github: FakeWeb) -> None:
    github.serve("/repos/octo/widget", as_json(REPO))
    github.serve("/repos/octo/widget/readme", text(README))


def test_github_repo_is_served_from_the_api_with_its_readme(
    client: TestClient, apis: Apis
) -> None:
    serve_repo(apis.github)

    status, body = scrape(client, "https://github.com/octo/widget")

    assert status == 200
    data = body["data"]
    assert data["siteAdapter"] == "github"
    assert data["fetcher"] == "github"
    assert data["verdict"] == "accepted"
    assert data["metadata"]["title"] == "octo/widget"
    assert data["metadata"]["sourceURL"] == "https://github.com/octo/widget"
    assert "A small widget library for tests" in data["markdown"]
    assert "1234 stars" in data["markdown"]
    assert "This paragraph stands in for real prose" in data["markdown"]
    assert data["publishedDate"] == "2026-11-20"
    seen = apis.github.requests[0].headers
    assert seen["authorization"] == f"Bearer {GITHUB_TOKEN}"


def test_github_issue_comes_with_its_comments(client: TestClient, apis: Apis) -> None:
    apis.github.serve(
        "/repos/octo/widget/issues/42",
        as_json(
            {
                "title": "Widget crashes on empty config",
                "state": "open",
                "user": {"login": "reporter"},
                "created_at": "2026-11-01T09:00:00Z",
                "labels": [{"name": "bug"}],
                "body": "Steps to reproduce: " + LOREM * 6,
            }
        ),
    )
    apis.github.serve(
        "/repos/octo/widget/issues/42/comments?per_page=100",
        as_json(
            [
                {
                    "user": {"login": "maintainer"},
                    "created_at": "2026-11-02T09:00:00Z",
                    "body": "Fixed in 4.2, thanks for the report.",
                }
            ]
        ),
    )

    status, body = scrape(client, "https://github.com/octo/widget/issues/42")

    assert status == 200
    data = body["data"]
    assert data["siteAdapter"] == "github"
    assert data["metadata"]["title"] == "Widget crashes on empty config (#42)"
    assert "opened by @reporter" in data["markdown"]
    assert "labels: bug" in data["markdown"]
    assert "### @maintainer on 2026-11-02" in data["markdown"]
    assert "Fixed in 4.2, thanks for the report." in data["markdown"]
    assert data["publishedDate"] == "2026-11-01"


def test_github_release_by_tag(client: TestClient, apis: Apis) -> None:
    apis.github.serve(
        "/repos/octo/widget/releases/tags/v4.2.0",
        as_json(
            {
                "name": "Widget 4.2",
                "tag_name": "v4.2.0",
                "published_at": "2026-11-10T12:00:00Z",
                "body": "## Changes\n\n" + LOREM * 8,
            }
        ),
    )

    status, body = scrape(client, "https://github.com/octo/widget/releases/tag/v4.2.0")

    assert status == 200
    data = body["data"]
    assert data["siteAdapter"] == "github"
    assert data["metadata"]["title"] == "octo/widget Widget 4.2"
    assert "Tag v4.2.0" in data["markdown"]
    assert "## Changes" in data["markdown"]
    assert data["publishedDate"] == "2026-11-10"


def test_github_file_is_served_raw_in_a_code_fence(client: TestClient, apis: Apis) -> None:
    source = "def widget():\n" + "".join(f"    step_{i} = {i}  # a line\n" for i in range(40))
    apis.github.serve("/repos/octo/widget/contents/src/widget.py?ref=main", text(source))

    status, body = scrape(client, "https://github.com/octo/widget/blob/main/src/widget.py")

    assert status == 200
    data = body["data"]
    assert data["siteAdapter"] == "github"
    assert data["metadata"]["title"] == "octo/widget/src/widget.py at main"
    assert "```py\ndef widget():" in data["markdown"]
    assert "step_39 = 39" in data["markdown"]
    assert apis.github.requests[0].headers["accept"] == "application/vnd.github.raw+json"


def test_github_file_with_an_encoded_name_is_found(client: TestClient, apis: Apis) -> None:
    apis.github.serve(
        "/repos/octo/widget/contents/docs/Getting%20Started.md?ref=main", text(README)
    )

    status, body = scrape(
        client, "https://github.com/octo/widget/blob/main/docs/Getting%20Started.md"
    )

    assert status == 200
    data = body["data"]
    assert data["siteAdapter"] == "github"
    assert data["metadata"]["title"] == "octo/widget/docs/Getting Started.md at main"
    assert "This paragraph stands in for real prose" in data["markdown"]


def test_github_pull_request_is_served_as_its_conversation(
    client: TestClient, apis: Apis
) -> None:
    apis.github.serve(
        "/repos/octo/widget/issues/7",
        as_json(
            {
                "title": "Add arm64 images",
                "state": "closed",
                "user": {"login": "contributor"},
                "created_at": "2026-10-01T09:00:00Z",
                "pull_request": {"url": "https://api.github.com/repos/octo/widget/pulls/7"},
                "body": LOREM * 6,
            }
        ),
    )
    apis.github.serve("/repos/octo/widget/issues/7/comments?per_page=100", as_json([]))

    status, body = scrape(client, "https://github.com/octo/widget/pull/7")

    assert status == 200
    assert body["data"]["siteAdapter"] == "github"
    assert "Pull request in octo/widget · closed" in body["data"]["markdown"]


def test_rejected_adapter_result_falls_back_to_the_ladder(
    client: TestClient, apis: Apis, firecrawl: FakeFirecrawl
) -> None:
    # The API knows the repo but has almost nothing to say: the Gate rejects it.
    apis.github.serve("/repos/octo/tiny", as_json({"full_name": "octo/tiny"}))
    url = "https://github.com/octo/tiny"
    firecrawl.serve(url, firecrawl_page("article.html", ARTICLE_MARKDOWN))

    status, body = scrape(client, url)

    assert status == 200
    data = body["data"]
    assert data["fetcher"] == "paid"
    assert data["siteAdapter"] is None
    assert [(a["fetcher"], a["verdict"]) for a in data["attempts"]] == [
        ("github", "rejected"),
        ("paid", "accepted"),
    ]


def test_failing_api_falls_back_to_the_ladder_and_a_failure_lists_both(
    client: TestClient, apis: Apis
) -> None:
    # The fake API answers 404 for everything; the fake Firecrawl has nothing either.
    status, body = scrape(client, "https://github.com/octo/gone")

    assert status == 502
    assert body["success"] is False
    assert [(a["fetcher"], a["reason"]) for a in body["attempts"]] == [
        ("github", "http_404"),
        ("paid", "fetch_error"),
    ]
    assert body["attempts"][0]["detail"] == "GitHub API answered 404"


def test_github_pages_the_adapter_doesnt_know_go_to_the_ladder(
    client: TestClient, apis: Apis, firecrawl: FakeFirecrawl
) -> None:
    url = "https://github.com/octo/widget/wiki/Setup"
    firecrawl.serve(url, firecrawl_page("article.html", ARTICLE_MARKDOWN))

    status, body = scrape(client, url)

    assert status == 200
    assert body["data"]["fetcher"] == "paid"
    assert [a["fetcher"] for a in body["data"]["attempts"]] == ["paid"]
    assert apis.github.requests == []


def test_adapter_result_is_cached_and_still_names_the_adapter(
    client: TestClient, apis: Apis
) -> None:
    serve_repo(apis.github)
    scrape(client, "https://github.com/octo/widget")

    status, body = scrape(client, "https://github.com/octo/widget")

    assert status == 200
    assert body["data"]["metadata"]["cacheState"] == "hit"
    assert body["data"]["siteAdapter"] == "github"
    assert apis.github.hits("/repos/octo/widget") == 1


def test_forcing_a_fetcher_skips_the_site_adapter(
    client: TestClient, apis: Apis, firecrawl: FakeFirecrawl
) -> None:
    serve_repo(apis.github)
    url = "https://github.com/octo/widget"
    firecrawl.serve(url, firecrawl_page("article.html", ARTICLE_MARKDOWN))

    status, body = scrape(client, url, fetcher="paid")

    assert status == 200
    assert body["data"]["fetcher"] == "paid"
    assert body["data"]["siteAdapter"] is None
    assert apis.github.requests == []


def test_forcing_a_site_adapter_gets_the_page_from_it(client: TestClient, apis: Apis) -> None:
    serve_repo(apis.github)

    status, body = scrape(client, "https://github.com/octo/widget", fetcher="github", maxAge=0)

    assert status == 200
    assert body["data"]["siteAdapter"] == "github"


def test_a_forced_site_adapter_that_fails_has_no_ladder_to_fall_back_to(
    client: TestClient, apis: Apis, firecrawl: FakeFirecrawl
) -> None:
    url = "https://github.com/octo/widget"  # the fake API answers 503
    apis.github.serve("/repos/octo/widget", FakeResponse(b"unavailable", 503))
    firecrawl.serve(url, firecrawl_page("article.html", ARTICLE_MARKDOWN))

    status, body = scrape(client, url, fetcher="github")

    assert status == 502
    assert [(a["fetcher"], a["reason"]) for a in body["attempts"]] == [("github", "http_503")]
    assert firecrawl.scrapes == []


def test_a_forced_site_adapter_refuses_a_url_it_does_not_handle(
    client: TestClient, apis: Apis
) -> None:
    status, body = scrape(client, "https://example.org/article", fetcher="youtube")

    assert status == 502
    assert body["attempts"][0]["reason"] == "unhandled_url"
    assert apis.youtube.requests == []


def test_forcing_a_site_adapter_that_is_not_enabled_is_refused(
    adapter_config: Config,
) -> None:
    adapter_config.adapters.enabled = ["github"]
    client = TestClient(create_app(adapter_config))

    status, body = scrape(client, "https://www.reddit.com/r/one/", fetcher="reddit")

    assert status == 422
    assert body["code"] == "UNKNOWN_FETCHER"


def adapter_rates(client: TestClient) -> dict[str, dict[str, Any]]:
    response = client.get("/health/adapters")
    assert response.status_code == 200
    return {r["adapter"]: r for r in response.json()["adapters"]}


def test_each_site_adapters_attempts_and_successes_are_counted(
    client: TestClient, apis: Apis, firecrawl: FakeFirecrawl
) -> None:
    serve_repo(apis.github)
    apis.github.serve("/repos/octo/broken", FakeResponse(b"unavailable", 503))
    firecrawl.serve("https://github.com/octo/broken", firecrawl_page("article.html", ARTICLE_MARKDOWN))
    scrape(client, "https://github.com/octo/widget")  # github: accepted
    scrape(client, "https://github.com/octo/broken")  # github: 503, then paid

    seen = adapter_rates(client)

    assert list(seen) == ["youtube", "reddit", "github"]
    assert (seen["github"]["attempts"], seen["github"]["successes"]) == (2, 1)
    assert seen["reddit"]["attempts"] == 0


def test_a_page_the_adapters_api_says_does_not_exist_is_not_held_against_it(
    client: TestClient,
) -> None:
    scrape(client, "https://github.com/octo/gone")  # the fake API answers 404

    assert adapter_rates(client)["github"]["attempts"] == 0


def test_a_site_adapter_skipped_for_a_cool_down_is_not_counted(
    client: TestClient, apis: Apis
) -> None:
    apis.reddit.serve("/r/one.json?raw_json=1", FakeResponse(b"slow down", 429))
    scrape(client, "https://www.reddit.com/r/one/")
    scrape(client, "https://www.reddit.com/r/two/")  # reddit skipped: cooling down

    reddit = adapter_rates(client)["reddit"]

    assert (reddit["attempts"], reddit["successes"]) == (1, 0)


def test_github_api_is_called_without_a_token_when_none_is_set(
    adapter_config: Config, apis: Apis
) -> None:
    adapter_config.adapters.github_token = None
    client = TestClient(create_app(adapter_config))
    serve_repo(apis.github)

    status, body = scrape(client, "https://github.com/octo/widget")

    assert status == 200
    assert body["data"]["siteAdapter"] == "github"
    assert "authorization" not in apis.github.requests[0].headers


# ── Reddit ────────────────────────────────────────────────────────────────


def reddit_post(**extra: Any) -> dict[str, Any]:
    return {
        "kind": "t3",
        "data": {
            "title": "What do you run on your Pi 5?",
            "subreddit": "raspberry_pi",
            "author": "tinkerer",
            "score": 321,
            "num_comments": 2,
            "created_utc": 1763380800,  # 2025-11-17
            "is_self": True,
            "selftext": "Curious what people host at home. " + LOREM * 4,
            "permalink": "/r/raspberry_pi/comments/abc123/what_do_you_run/",
            **extra,
        },
    }


def comment(author: str, body: str, *replies: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "t1",
        "data": {
            "author": author,
            "score": 10,
            "body": body,
            # Reddit sends "" for a comment without replies.
            "replies": listing(*replies) if replies else "",
        },
    }


def listing(*children: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "Listing", "data": {"children": list(children)}}


def test_reddit_post_comes_from_the_json_view_with_nested_comments(
    client: TestClient, apis: Apis
) -> None:
    apis.reddit.serve(
        "/r/raspberry_pi/comments/abc123/what_do_you_run.json?raw_json=1",
        as_json(
            [
                listing(reddit_post()),
                listing(
                    comment(
                        "homelabber",
                        "Pi-hole and a web scraper.",
                        comment("tinkerer", "Which scraper?"),
                    ),
                    {"kind": "more", "data": {"children": ["x"]}},
                ),
            ]
        ),
    )

    status, body = scrape(
        client, "https://www.reddit.com/r/raspberry_pi/comments/abc123/what_do_you_run/"
    )

    assert status == 200
    data = body["data"]
    assert data["siteAdapter"] == "reddit"
    assert data["metadata"]["title"] == "What do you run on your Pi 5?"
    assert "r/raspberry_pi · u/tinkerer · 321 points" in data["markdown"]
    assert "Curious what people host at home." in data["markdown"]
    assert "**u/homelabber** · 10 points\nPi-hole and a web scraper." in data["markdown"]
    assert "> **u/tinkerer** · 10 points\n> Which scraper?" in data["markdown"]
    assert data["publishedDate"] == "2025-11-17"
    assert "gtt-web-scraper" in apis.reddit.requests[0].headers["user-agent"]


def test_reddit_listing_keeps_the_query(client: TestClient, apis: Apis) -> None:
    apis.reddit.serve(
        "/r/raspberry_pi/top.json?t=week&raw_json=1",
        as_json(listing(reddit_post(), reddit_post(title="Pi 5 cluster build log"))),
    )

    status, body = scrape(client, "https://old.reddit.com/r/raspberry_pi/top/?t=week")

    assert status == 200
    data = body["data"]
    assert data["siteAdapter"] == "reddit"
    assert "## [Pi 5 cluster build log](https://www.reddit.com/r/raspberry_pi/" in data["markdown"]


def test_reddit_short_link_is_the_post(client: TestClient, apis: Apis) -> None:
    apis.reddit.serve(
        "/comments/abc123.json?raw_json=1", as_json([listing(reddit_post()), listing()])
    )

    status, body = scrape(client, "https://redd.it/abc123")

    assert status == 200
    assert body["data"]["siteAdapter"] == "reddit"
    assert body["data"]["metadata"]["title"] == "What do you run on your Pi 5?"


def test_reddit_refusing_the_json_view_falls_back_to_the_ladder(
    client: TestClient, apis: Apis, firecrawl: FakeFirecrawl
) -> None:
    url = "https://www.reddit.com/r/raspberry_pi/"
    apis.reddit.serve("/r/raspberry_pi.json?raw_json=1", FakeResponse(b"slow down", 429))
    firecrawl.serve(url, firecrawl_page("article.html", ARTICLE_MARKDOWN))

    status, body = scrape(client, url)

    assert status == 200
    assert body["data"]["fetcher"] == "paid"
    assert body["data"]["attempts"][0]["detail"] == "Reddit answered 429"


def test_reddit_refusing_the_json_view_puts_reddit_on_a_cool_down(
    client: TestClient, apis: Apis, firecrawl: FakeFirecrawl
) -> None:
    # Reddit's JSON view goes over the home connection: a 429 is a block like any other.
    first, second = "https://www.reddit.com/r/one/", "https://www.reddit.com/r/two/"
    apis.reddit.serve("/r/one.json?raw_json=1", FakeResponse(b"slow down", 429))
    apis.reddit.serve("/r/two.json?raw_json=1", as_json(listing(reddit_post())))
    for url in (first, second):
        firecrawl.serve(url, firecrawl_page("article.html", ARTICLE_MARKDOWN))
    scrape(client, first)

    status, body = scrape(client, second)

    assert status == 200
    assert body["data"]["fetcher"] == "paid"
    assert body["data"]["attempts"][0]["fetcher"] == "reddit"
    assert body["data"]["attempts"][0]["reason"] == "cooling_down"
    assert apis.reddit.hits("/r/two.json?raw_json=1") == 0


def test_reddit_front_page(client: TestClient, apis: Apis) -> None:
    apis.reddit.serve("/.json?raw_json=1", as_json(listing(reddit_post(), reddit_post())))

    status, body = scrape(client, "https://www.reddit.com/")

    assert status == 200
    assert body["data"]["siteAdapter"] == "reddit"
    assert body["data"]["metadata"]["title"] == "Reddit front page"


# ── YouTube ───────────────────────────────────────────────────────────────

CHANNEL_ID = "UCabcdefghijklmnopqrstuv"
FEED_PATH = f"/feeds/videos.xml?channel_id={CHANNEL_ID}"


def feed_entry(video_id: str, title: str, published: str, description: str) -> str:
    return f"""
  <entry>
    <id>yt:video:{video_id}</id>
    <yt:videoId>{video_id}</yt:videoId>
    <yt:channelId>{CHANNEL_ID}</yt:channelId>
    <title>{title}</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v={video_id}"/>
    <author><name>G Talks Tech</name></author>
    <published>{published}</published>
    <updated>{published}</updated>
    <media:group>
      <media:title>{title}</media:title>
      <media:description>{description}</media:description>
      <media:community>
        <media:starRating count="12" average="5.00" min="1" max="5"/>
        <media:statistics views="4567"/>
      </media:community>
    </media:group>
  </entry>"""


FEED = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
  <id>yt:channel:{CHANNEL_ID}</id>
  <yt:channelId>{CHANNEL_ID}</yt:channelId>
  <title>G Talks Tech</title>
  <author><name>G Talks Tech</name></author>
  <published>2020-01-01T00:00:00+00:00</published>
  {feed_entry("aaaaaaaaaaa", "Self-hosting a scraper on a Pi 5", "2026-11-20T15:00:00+00:00",
              "In this video we build a scraper. " + LOREM * 5)}
  {feed_entry("bbbbbbbbbbb", "Tailscale exit nodes explained", "2026-11-13T15:00:00+00:00",
              "Exit nodes, subnet routers and more. " + LOREM * 2)}
</feed>"""


def test_youtube_channel_is_served_from_its_feed(client: TestClient, apis: Apis) -> None:
    apis.youtube.serve(FEED_PATH, text(FEED, "application/atom+xml"))

    status, body = scrape(client, f"https://www.youtube.com/channel/{CHANNEL_ID}/videos")

    assert status == 200
    data = body["data"]
    assert data["siteAdapter"] == "youtube"
    assert data["metadata"]["title"] == "G Talks Tech"
    assert (
        "## [Self-hosting a scraper on a Pi 5](https://www.youtube.com/watch?v=aaaaaaaaaaa)"
        in data["markdown"]
    )
    assert "Exit nodes, subnet routers and more." in data["markdown"]
    assert "4567 views" in data["markdown"]
    assert data["publishedDate"] == "2026-11-20"


def test_youtube_handle_is_resolved_to_its_channel_feed(client: TestClient, apis: Apis) -> None:
    apis.youtube.serve(
        "/@GTalksTech",
        text(
            '<html><head><link rel="canonical"'
            f' href="https://www.youtube.com/channel/{CHANNEL_ID}"></head></html>',
            "text/html",
        ),
    )
    apis.youtube.serve(FEED_PATH, text(FEED, "application/atom+xml"))

    status, body = scrape(client, "https://www.youtube.com/@GTalksTech")

    assert status == 200
    assert body["data"]["siteAdapter"] == "youtube"
    assert "Tailscale exit nodes explained" in body["data"]["markdown"]


def serve_video(youtube: FakeWeb, video_id: str) -> None:
    watch = f"https://www.youtube.com/watch?v={video_id}"
    youtube.serve(
        "/oembed?" + urlencode({"url": watch, "format": "json"}),
        as_json(
            {
                "title": "Self-hosting a scraper on a Pi 5",
                "author_name": "G Talks Tech",
                "author_url": "https://www.youtube.com/@GTalksTech",
            }
        ),
    )
    youtube.serve(
        "/@GTalksTech",
        text(f'<script>var data = {{"externalId":"{CHANNEL_ID}"}};</script>', "text/html"),
    )
    youtube.serve(FEED_PATH, text(FEED, "application/atom+xml"))


def test_youtube_video_is_served_from_its_channel_feed(client: TestClient, apis: Apis) -> None:
    serve_video(apis.youtube, "aaaaaaaaaaa")

    status, body = scrape(client, "https://youtu.be/aaaaaaaaaaa")

    assert status == 200
    data = body["data"]
    assert data["siteAdapter"] == "youtube"
    assert data["metadata"]["title"] == "Self-hosting a scraper on a Pi 5"
    assert (
        "YouTube video by [G Talks Tech](https://www.youtube.com/@GTalksTech)" in data["markdown"]
    )
    assert "In this video we build a scraper." in data["markdown"]
    assert "Tailscale exit nodes" not in data["markdown"]
    assert data["publishedDate"] == "2026-11-20"


def test_youtube_video_missing_from_the_feed_falls_back_to_the_ladder(
    client: TestClient, apis: Apis, firecrawl: FakeFirecrawl
) -> None:
    serve_video(apis.youtube, "zzzzzzzzzzz")
    url = "https://www.youtube.com/watch?v=zzzzzzzzzzz"
    firecrawl.serve(url, firecrawl_page("article.html", ARTICLE_MARKDOWN))

    status, body = scrape(client, url)

    assert status == 200
    data = body["data"]
    assert data["fetcher"] == "paid"
    assert data["attempts"][0]["fetcher"] == "youtube"
    assert "isn't among its channel's newest uploads" in data["attempts"][0]["detail"]
