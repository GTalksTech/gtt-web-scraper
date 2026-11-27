from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from scraper.api import create_app
from scraper.config import load_config

from .fakeweb import FakeWeb


@pytest.fixture
def web() -> Iterator[FakeWeb]:
    fake = FakeWeb()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def client() -> TestClient:
    config = load_config()
    # Keep challenge polling instant in tests; the count is what matters.
    config.gate.challenge_poll_interval_s = 0.0
    return TestClient(create_app(config))
