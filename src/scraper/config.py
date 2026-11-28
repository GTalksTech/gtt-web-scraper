"""Service config, loaded from YAML (default: config/scraper.yaml, or $SCRAPER_CONFIG)."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from .fetchers.chrome import Engine

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
class ChromeConfig:
    # Real Chrome, in a visible window. On the Pi the window is on a virtual display
    # (Xvfb, started by the image); headless Chrome is easier for sites to spot.
    headless: bool = False
    # The browser profile (cookies, cache, history) kept between pages and restarts, so
    # the browser looks like a returning visitor. A relative path is from the repo root;
    # in the container it is in the cache volume.
    profile_dir: str = "data/chrome-profile"
    # Timezone and locale the browser reports. They match the home connection, whose
    # address sites can place.
    timezone: str = "America/New_York"
    locale: str = "en-US"
    # Pages open at once; more requests wait for one to close.
    max_pages: int = 2
    # The browser is closed and started again after this many pages, before memory
    # creeps up.
    restart_after_pages: int = 50
    # How long one page may take to load.
    timeout_s: float = 30.0
    # After the page has loaded, how long to wait for its scripts to finish fetching.
    settle_s: float = 5.0
    # Makes the Engine the Chrome Fetcher drives. None for real Chrome through
    # Patchright; tests put a fake here. Never from the YAML file.
    engine: Callable[[ChromeConfig], Engine] | None = None

    def resolved_profile_dir(self) -> Path:
        path = Path(self.profile_dir)
        return path if path.is_absolute() else REPO_ROOT / path


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
    # From the environment (TINYFISH_API_KEY, EXA_API_KEY, TAVILY_API_KEY), never from
    # the YAML file. A Provider without its key is skipped. DuckDuckGo needs none.
    tinyfish_api_key: str | None = None
    exa_api_key: str | None = None
    tavily_api_key: str | None = None


@dataclass
class HealthConfig:
    # Rolling success rates per Fetcher, Search Provider and Site Adapter are counted
    # over this window.
    window_s: float = 86400.0
    # One with fewer attempts than this in the window isn't judged.
    min_attempts: int = 10
    # One whose share of successful attempts falls below this raises an Incident.
    min_success_rate: float = 0.5
    # When at least this share of the Canary Set's checks fail, it is one widespread
    # Incident rather than one per Site, Site Adapter or Search Provider.
    widespread_fraction: float = 0.5


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


# The Canary Set's file, always beside the config file, for the same reason as the
# Source List's: a policy check diffs it to catch a removed or loosened entry (ADR 0002).
CANARY_SET_FILE = "canary.yaml"


def config_path() -> Path:
    """The config file in use: $SCRAPER_CONFIG, or config/scraper.yaml."""
    return Path(os.environ.get("SCRAPER_CONFIG") or DEFAULT_PATH)


# The Source List's file, always beside the config file. It is deliberately not a
# setting: a policy check diffs this one file to catch a removed domain (ADR 0002), and
# a setting could point search at a different, shorter list.
SOURCE_LIST_FILE = "source-list.yaml"


def load_source_list(path: Path) -> SourceList:
    """The Source List in the YAML file at `path` (lists `trusted` and `blocked`)."""
    raw: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
    return SourceList(trusted=list(raw.get("trusted") or []), blocked=list(raw.get("blocked") or []))


@dataclass
class Config:
    ladder: list[str] = field(default_factory=lambda: ["plain", "chrome", "paid"])
    plain: PlainConfig = field(default_factory=PlainConfig)
    chrome: ChromeConfig = field(default_factory=ChromeConfig)
    paid: PaidConfig = field(default_factory=PaidConfig)
    gate: GateConfig = field(default_factory=GateConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    sites: SitesConfig = field(default_factory=SitesConfig)
    adapters: AdaptersConfig = field(default_factory=AdaptersConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    source_list: SourceList = field(default_factory=SourceList)
    health: HealthConfig = field(default_factory=HealthConfig)


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path or config_path())
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
        chrome=ChromeConfig(**raw.get("chrome", {})),
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
        source_list=load_source_list(path.parent / SOURCE_LIST_FILE),
        health=HealthConfig(**raw.get("health", {})),
    )
