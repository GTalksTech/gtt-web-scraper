"""The YouTube Site Adapter: channels, playlists and videos from YouTube's public feeds.

A channel or playlist comes back as its latest uploads (the feed holds the newest 15),
each with its description. A video comes back from its channel's feed, found through
oEmbed (title and channel); a video that is no longer among its channel's newest 15
uploads has no description in any public feed, so it goes to the Ladder.

Handled URLs (youtube.com, www., m., and youtu.be):
  /watch?v={id}, /shorts/{id}, /live/{id}, /embed/{id}, youtu.be/{id}   a video
  /channel/{UC…}, /@{handle}, /c/{name}, /user/{name} (and their tabs)  a channel
  /playlist?list={id}                                                   a playlist

A channel known only by handle or custom name is resolved to its channel ID from the
channel page's canonical link, the one thing the feeds can't tell us.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from ..config import AdaptersConfig
from ..fetchers import FetchError
from ..page import FetchedPage
from . import adapter_page, get, get_json, iso_date

_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com")
_VIDEO_ID = re.compile(r"^[\w-]{11}$")
_CHANNEL_ID = re.compile(r"(UC[\w-]{22})")
_CANONICAL = re.compile(
    r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"'
)
# The channel's own ID in the page's data ("channelId" also names other channels there).
_EXTERNAL_ID = re.compile(r'"externalId":"(UC[\w-]{22})"')
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}
_PUBLIC = "https://www.youtube.com"


@dataclass(frozen=True)
class _Target:
    kind: str  # "video", "channel_id", "channel_page", "user", "playlist"
    value: str  # video ID, channel ID, channel page path, user name or playlist ID


def _target(url: str) -> _Target | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    segments = [s for s in parts.path.split("/") if s]
    query = parse_qs(parts.query)
    if host == "youtu.be":
        if len(segments) == 1 and _VIDEO_ID.match(segments[0]):
            return _Target("video", segments[0])
        return None
    if host not in _HOSTS or not segments:
        return None
    first = segments[0]
    if first == "watch":
        video = (query.get("v") or [""])[0]
        return _Target("video", video) if _VIDEO_ID.match(video) else None
    if first in ("shorts", "live", "embed") and len(segments) >= 2:
        return _Target("video", segments[1]) if _VIDEO_ID.match(segments[1]) else None
    if first == "playlist":
        playlist = (query.get("list") or [""])[0]
        return _Target("playlist", playlist) if playlist else None
    if first == "channel" and len(segments) >= 2 and _CHANNEL_ID.fullmatch(segments[1]):
        return _Target("channel_id", segments[1])
    if first == "user" and len(segments) >= 2:
        return _Target("user", segments[1])
    if first.startswith("@") and len(first) > 1:
        return _Target("channel_page", f"/{first}")
    if first == "c" and len(segments) >= 2:
        return _Target("channel_page", f"/c/{segments[1]}")
    return None


@dataclass(frozen=True)
class _Entry:
    video_id: str
    title: str
    link: str
    published: str | None
    description: str
    views: str | None


@dataclass(frozen=True)
class _Feed:
    title: str
    author: str
    entries: list[_Entry]


class YouTubeAdapter:
    name = "youtube"

    def __init__(self, config: AdaptersConfig) -> None:
        self._config = config

    def handles(self, url: str) -> bool:
        return _target(url) is not None

    def fetch(self, url: str) -> FetchedPage:
        target = _target(url)
        if target is None:
            raise FetchError(f"YouTube adapter doesn't handle {url}")
        if target.kind == "video":
            return self._video(url, target.value)
        if target.kind == "playlist":
            feed = self._feed({"playlist_id": target.value})
        elif target.kind == "user":
            feed = self._feed({"user": target.value})
        elif target.kind == "channel_page":
            feed = self._feed({"channel_id": self._channel_id(target.value)})
        else:
            feed = self._feed({"channel_id": target.value})
        return _feed_page(url, feed)

    def _video(self, url: str, video_id: str) -> FetchedPage:
        watch = f"{_PUBLIC}/watch?v={video_id}"
        oembed = get_json(
            self._url("/oembed", {"url": watch, "format": "json"}),
            "YouTube oEmbed",
            self._config.timeout_s,
        )
        author_url = str(oembed.get("author_url") or "")
        channel = _CHANNEL_ID.search(author_url)
        channel_id = channel.group(1) if channel else self._channel_id(urlsplit(author_url).path)
        feed = self._feed({"channel_id": channel_id})
        entry = next((e for e in feed.entries if e.video_id == video_id), None)
        if entry is None:
            raise FetchError(
                f"video {video_id} isn't among its channel's newest uploads in the public"
                " feed, which is the only public source of its description"
            )
        author = str(oembed.get("author_name") or feed.author)
        byline = [f"YouTube video by [{author}]({author_url or _PUBLIC})"]
        if entry.published:
            byline.append(f"published {entry.published}")
        if entry.views:
            byline.append(f"{entry.views} views")
        markdown = "\n".join([f"# {entry.title}", "", " · ".join(byline), "", entry.description])
        return adapter_page(url, markdown, entry.title, entry.published)

    def _channel_id(self, page_path: str) -> str:
        """The channel ID behind a handle or custom-name page, from its canonical link."""
        if not page_path.startswith("/"):
            raise FetchError(f"can't tell which YouTube channel {page_path!r} is")
        page = get(
            self._url(quote(page_path, safe="/@")), "YouTube channel page", self._config.timeout_s
        )
        html = str(page.text)
        found = _CANONICAL.search(html) or _EXTERNAL_ID.search(html)
        if found is None:
            raise FetchError(f"YouTube channel page {page_path} names no channel ID")
        return found.group(1)

    def _feed(self, params: dict[str, str]) -> _Feed:
        response = get(
            self._url("/feeds/videos.xml", params), "YouTube feed", self._config.timeout_s
        )
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise FetchError(f"YouTube feed isn't valid XML: {exc}") from exc
        return _Feed(
            title=root.findtext("atom:title", "", _NS).strip(),
            author=root.findtext("atom:author/atom:name", "", _NS).strip(),
            entries=[_entry(e) for e in root.findall("atom:entry", _NS)],
        )

    def _url(self, path: str, params: dict[str, str] | None = None) -> str:
        query = f"?{urlencode(params)}" if params else ""
        return f"{self._config.youtube_url.rstrip('/')}{path}{query}"


def _entry(element: ET.Element) -> _Entry:
    video_id = element.findtext("yt:videoId", "", _NS).strip()
    link = element.find("atom:link", _NS)
    stats = element.find("media:group/media:community/media:statistics", _NS)
    return _Entry(
        video_id=video_id,
        title=element.findtext("atom:title", "", _NS).strip(),
        link=link.get("href", "") if link is not None else f"{_PUBLIC}/watch?v={video_id}",
        published=iso_date(element.findtext("atom:published", "", _NS)),
        description=element.findtext("media:group/media:description", "", _NS).strip(),
        views=stats.get("views") if stats is not None else None,
    )


def _feed_page(url: str, feed: _Feed) -> FetchedPage:
    lines = [f"# {feed.title}", "", f"Latest uploads from YouTube, newest first ({feed.author})."]
    for entry in feed.entries:
        facts = [f for f in (entry.published, entry.views and f"{entry.views} views") if f]
        lines += ["", f"## [{entry.title}]({entry.link})", "", " · ".join(facts)]
        if entry.description:
            lines += ["", entry.description]
    newest = feed.entries[0].published if feed.entries else None
    return adapter_page(url, "\n".join(lines), feed.title, newest)
