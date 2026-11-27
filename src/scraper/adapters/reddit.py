"""The Reddit Site Adapter: any reddit.com page from Reddit's JSON view of it (the same
path with ".json" added).

A post comes back with its comments (up to _MAX_COMMENTS, nested up to _MAX_DEPTH); a
listing (a subreddit, a user, the front page) comes back as its posts and comments.
Short links (redd.it/{id}) become the post. Share links (/r/{sub}/s/{code}) only
redirect, so they go to the Ladder.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from ..config import AdaptersConfig
from ..fetchers import FetchError
from ..page import FetchedPage
from . import adapter_page, epoch_date, get_json

_MAX_COMMENTS = 100
_MAX_DEPTH = 4
_LISTING_EXCERPT = 600  # characters of each post's text shown in a listing
_PUBLIC = "https://www.reddit.com"  # links in the markdown point at Reddit itself


def _json_path(url: str) -> str | None:
    """The path and query of the JSON view of a Reddit URL this adapter handles."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/")
    if host == "redd.it":
        post_id = path.strip("/")
        if not post_id or "/" in post_id:
            return None
        path = f"/comments/{post_id}"
    elif not (host == "reddit.com" or host.endswith(".reddit.com")):
        return None
    if path.split("/")[3:4] == ["s"] or path.endswith(".json"):
        return None
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "raw_json"]
    return f"{path}.json?{urlencode([*query, ('raw_json', '1')])}"


class RedditAdapter:
    name = "reddit"

    def __init__(self, config: AdaptersConfig) -> None:
        self._config = config

    def handles(self, url: str) -> bool:
        return _json_path(url) is not None

    def fetch(self, url: str) -> FetchedPage:
        path = _json_path(url)
        if path is None:
            raise FetchError(f"Reddit adapter doesn't handle {url}")
        body = get_json(
            f"{self._config.reddit_url.rstrip('/')}{path}", "Reddit", self._config.timeout_s
        )
        if isinstance(body, list) and body and _children(body[0]):
            post = _children(body[0])[0]
            if post.get("kind") == "t3":
                comments = _children(body[1]) if len(body) > 1 else []
                return _post_page(url, post["data"], comments)
        if isinstance(body, dict) and body.get("kind") == "Listing":
            return _listing_page(url, _children(body))
        raise FetchError("Reddit answered with JSON that is neither a post nor a listing")


def _children(listing: Any) -> list[dict[str, Any]]:
    if not isinstance(listing, dict):
        return []
    children = (listing.get("data") or {}).get("children")
    return children if isinstance(children, list) else []


def _byline(item: dict[str, Any]) -> str:
    facts = [
        f"r/{item.get('subreddit')}",
        f"u/{item.get('author')}",
        f"{item.get('score', 0)} points",
    ]
    if "num_comments" in item:
        facts.append(f"{item['num_comments']} comments")
    if epoch_date(item.get("created_utc")):
        facts.append(str(epoch_date(item.get("created_utc"))))
    return " · ".join(facts)


def _post_page(url: str, post: dict[str, Any], comments: list[dict[str, Any]]) -> FetchedPage:
    title = str(post.get("title") or "")
    lines = [f"# {title}", "", _byline(post), ""]
    if post.get("selftext"):
        lines += [str(post["selftext"]).strip(), ""]
    if not post.get("is_self") and post.get("url"):
        lines += [f"Link: {post['url']}", ""]
    rendered: list[str] = []
    _render_comments(comments, 0, rendered)
    if rendered:
        lines += ["## Comments", "", *rendered]
    return adapter_page(url, "\n".join(lines), title, epoch_date(post.get("created_utc")))


def _render_comments(
    children: list[dict[str, Any]], depth: int, out: list[str], left: int = _MAX_COMMENTS
) -> int:
    """Append up to `left` comments as markdown, nesting replies as deeper block
    quotes. Returns how many more may still be appended."""
    for child in children:
        if left <= 0:
            break
        if child.get("kind") != "t1":
            continue  # "more": comments the JSON view left out
        comment = child.get("data") or {}
        quote = "> " * depth
        out.append(f"{quote}**u/{comment.get('author')}** · {comment.get('score', 0)} points")
        for line in str(comment.get("body") or "").strip().splitlines():
            out.append(f"{quote}{line}".rstrip())
        out.append("")
        left -= 1
        replies = comment.get("replies")
        if depth + 1 < _MAX_DEPTH and isinstance(replies, dict):
            left = _render_comments(_children(replies), depth + 1, out, left)
    return left


def _listing_page(url: str, children: list[dict[str, Any]]) -> FetchedPage:
    title = urlsplit(url).path.strip("/") or "Reddit front page"
    lines = [f"# {title}"]
    for child in children:
        item = child.get("data") or {}
        link = f"{_PUBLIC}{item.get('permalink', '')}"
        if child.get("kind") == "t3":
            lines += ["", f"## [{item.get('title')}]({link})", "", _byline(item)]
            text = str(item.get("selftext") or "").strip()
            if text:
                excerpt = text[:_LISTING_EXCERPT] + ("…" if len(text) > _LISTING_EXCERPT else "")
                lines += ["", excerpt]
        elif child.get("kind") == "t1":
            lines += [
                "",
                f"## Comment on [{item.get('link_title')}]({link})",
                "",
                _byline(item),
                "",
                str(item.get("body") or "").strip(),
            ]
    return adapter_page(url, "\n".join(lines), title, None)
