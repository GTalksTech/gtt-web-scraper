from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scraper.api import create_app
from scraper.config import Config, load_config

from .fakechrome import FakeChrome
from .fakefirecrawl import API_KEY, FakeFirecrawl
from .fakesearch import KEYS, PROVIDERS, FakeSearchProviders
from .fakeweb import FakeWeb


@pytest.fixture
def web() -> Iterator[FakeWeb]:
    fake = FakeWeb()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def firecrawl() -> Iterator[FakeFirecrawl]:
    fake = FakeFirecrawl()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def providers() -> Iterator[FakeSearchProviders]:
    fake = FakeSearchProviders()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def chrome() -> FakeChrome:
    return FakeChrome()


@pytest.fixture
def config(
    firecrawl: FakeFirecrawl, providers: FakeSearchProviders, chrome: FakeChrome, tmp_path: Path
) -> Config:
    config = load_config()
    # Each test gets its own empty page cache.
    config.cache.path = str(tmp_path / "scraper.db")
    # Fixed here so tests don't depend on tuning in config/scraper.yaml; polling is instant.
    config.gate.challenge_poll_attempts = 3
    config.gate.challenge_poll_interval_s = 0.0
    # No spacing between requests unless a test asks for it: the fake web is one site.
    config.sites.min_interval_s = 0.0
    # The Chrome Fetcher drives a fake Engine, never real Chrome.
    config.chrome.engine = chrome.engine
    config.chrome.profile_dir = str(tmp_path / "chrome-profile")
    # The Paid Fetcher talks to the fake Firecrawl, never the real one.
    config.paid.base_url = firecrawl.base_url
    config.paid.api_key = API_KEY
    # Search goes to the fake Search Providers, never the real ones.
    config.search.providers = list(PROVIDERS)
    for name in PROVIDERS:
        setattr(config.search, f"{name}_url", providers.base_url(name))
    config.search.tinyfish_api_key = KEYS["tinyfish"]
    config.search.exa_api_key = KEYS["exa"]
    config.search.tavily_api_key = KEYS["tavily"]
    return config


@pytest.fixture
def client(config: Config) -> TestClient:
    return TestClient(create_app(config))
