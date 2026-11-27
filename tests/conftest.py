from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from scraper.api import create_app
from scraper.config import Config, load_config

from .fakefirecrawl import API_KEY, FakeFirecrawl
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
def config(firecrawl: FakeFirecrawl) -> Config:
    config = load_config()
    # Fixed here so tests don't depend on tuning in config/scraper.yaml; polling is instant.
    config.gate.challenge_poll_attempts = 3
    config.gate.challenge_poll_interval_s = 0.0
    # The Paid Fetcher talks to the fake Firecrawl, never the real one.
    config.paid.base_url = firecrawl.base_url
    config.paid.api_key = API_KEY
    return config


@pytest.fixture
def client(config: Config) -> TestClient:
    return TestClient(create_app(config))
