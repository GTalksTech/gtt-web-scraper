"""Service config, loaded from YAML (default: config/scraper.yaml, or $SCRAPER_CONFIG)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = REPO_ROOT / "config" / "scraper.yaml"


@dataclass
class PlainConfig:
    impersonate: str = "chrome"
    timeout_s: float = 20.0


@dataclass
class PaidConfig:
    base_url: str = "https://api.firecrawl.dev"
    timeout_s: float = 60.0
    # After Firecrawl reports the quota exhausted, the Paid Fetcher stays unavailable
    # this long before one request checks again (a refused request costs no credits).
    quota_recheck_s: float = 21600.0
    # From the environment (FIRECRAWL_API_KEY), never from the YAML file.
    api_key: str | None = None


@dataclass
class GateConfig:
    min_chars: int = 200
    accepted_chars: int = 1000
    challenge_poll_attempts: int = 3
    challenge_poll_interval_s: float = 3.0


@dataclass
class CacheConfig:
    # SQLite file holding the page cache and per-site memory; a relative path is from
    # the repo root.
    path: str = "data/scraper.db"
    # How old a cached page may be when the Caller doesn't pass a maximum age.
    default_max_age_s: float = 86400.0

    def resolved_path(self) -> Path:
        path = Path(self.path)
        return path if path.is_absolute() else REPO_ROOT / path


@dataclass
class SitesConfig:
    # A site is a URL's host name. Fetchers that use the home connection (the plain
    # download) wait at least this long between requests to the same site.
    min_interval_s: float = 2.0
    # After a site blocks the home connection (403, 429 or a challenge), Fetchers that
    # use it skip the site this long, so requests go straight to the Paid Fetcher.
    cooldown_s: float = 1800.0
    # How long the Fetcher that last succeeded on a site is trusted to go first. After
    # this, one request climbs the Ladder from the bottom again, so a site that needed
    # the Paid Fetcher once doesn't spend quota on it forever.
    memory_ttl_s: float = 604800.0


@dataclass
class AdaptersConfig:
    # Site Adapters in use; a URL one of them handles skips the Ladder (unless its
    # result is rejected). Remove a name to send that site's URLs to the Ladder.
    enabled: list[str] = field(default_factory=lambda: ["youtube", "reddit", "github"])
    timeout_s: float = 20.0
    # Where each adapter sends its requests. Tests point these at fake endpoints.
    youtube_url: str = "https://www.youtube.com"
    reddit_url: str = "https://www.reddit.com"
    github_api_url: str = "https://api.github.com"
    # From the environment (GITHUB_API_TOKEN), never from the YAML file. Without it
    # the GitHub adapter calls the API unauthenticated (60 requests an hour).
    github_token: str | None = None


@dataclass
class SearchConfig:
    # Search Providers in the order a query tries them. A failing or out-of-quota
    # Provider passes the query to the next. Firecrawl is never one of them.
    providers: list[str] = field(
        default_factory=lambda: ["tinyfish", "exa", "tavily", "duckduckgo"]
    )
    timeout_s: float = 15.0
    # How long a query's results are served from the search cache.
    cache_max_age_s: float = 86400.0
    # Where each Provider is reached. Tests point these at fake Providers.
    tinyfish_url: str = "https://api.search.tinyfish.ai"
    exa_url: str = "https://api.exa.ai"
    tavily_url: str = "https://api.tavily.com"
    duckduckgo_url: str = "https://html.duckduckgo.com"
    # The Source List's file, relative to this config file's directory.
    source_list: str = "source-list.yaml"
    # From the environment (TINYFISH_API_KEY, EXA_API_KEY, TAVILY_API_KEY), never from
    # the YAML file. A Provider without its key is skipped. DuckDuckGo needs none.
    tinyfish_api_key: str | None = None
    exa_api_key: str | None = None
    tavily_api_key: str | None = None


# A domain name: dot-separated labels of letters, digits and hyphens.
_DOMAIN = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")


def _domain(entry: object) -> str:
    """A Source List entry as a bare lower-case domain ("www.GitHub.com." → "github.com")."""
    domain = str(entry or "").strip().lower().rstrip(".")
    domain = domain.removeprefix("www.")
    if not _DOMAIN.match(domain):
        raise ValueError(
            f"Source List entry {entry!r} is not a domain name (write e.g. github.com, "
            "with no scheme or path)"
        )
    return domain


@dataclass
class SourceList:
    """The domains search trusts (boosted) and blocks (dropped), with their subdomains.
    It shapes which sources Callers see; it does not judge whether a claim is true."""

    trusted: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.trusted = list(dict.fromkeys(_domain(d) for d in self.trusted))
        self.blocked = list(dict.fromkeys(_domain(d) for d in self.blocked))
        both = sorted(set(self.trusted) & set(self.blocked))
        if both:
            raise ValueError(f"Source List has domains both trusted and blocked: {', '.join(both)}")


def load_source_list(path: Path) -> SourceList:
    raw: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
    return SourceList(trusted=list(raw.get("trusted") or []), blocked=list(raw.get("blocked") or []))


@dataclass
class Config:
    ladder: list[str] = field(default_factory=lambda: ["plain", "paid"])
    plain: PlainConfig = field(default_factory=PlainConfig)
    paid: PaidConfig = field(default_factory=PaidConfig)
    gate: GateConfig = field(default_factory=GateConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    sites: SitesConfig = field(default_factory=SitesConfig)
    adapters: AdaptersConfig = field(default_factory=AdaptersConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    source_list: SourceList = field(default_factory=SourceList)


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path or os.environ.get("SCRAPER_CONFIG") or DEFAULT_PATH)
    raw: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
    search = SearchConfig(
        **raw.get("search", {}),
        tinyfish_api_key=os.environ.get("TINYFISH_API_KEY") or None,
        exa_api_key=os.environ.get("EXA_API_KEY") or None,
        tavily_api_key=os.environ.get("TAVILY_API_KEY") or None,
    )
    return Config(
        ladder=list(raw.get("ladder", Config().ladder)),
        plain=PlainConfig(**raw.get("plain", {})),
        paid=PaidConfig(
            **raw.get("paid", {}), api_key=os.environ.get("FIRECRAWL_API_KEY") or None
        ),
        gate=GateConfig(**raw.get("gate", {})),
        cache=CacheConfig(**raw.get("cache", {})),
        sites=SitesConfig(**raw.get("sites", {})),
        adapters=AdaptersConfig(
            **raw.get("adapters", {}), github_token=os.environ.get("GITHUB_API_TOKEN") or None
        ),
        search=search,
        source_list=load_source_list(path.parent / search.source_list),
    )
