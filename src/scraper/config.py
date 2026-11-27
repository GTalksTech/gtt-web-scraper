"""Service config, loaded from YAML (default: config/scraper.yaml, or $SCRAPER_CONFIG)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "scraper.yaml"


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
class Config:
    ladder: list[str] = field(default_factory=lambda: ["plain"])
    plain: PlainConfig = field(default_factory=PlainConfig)
    paid: PaidConfig = field(default_factory=PaidConfig)
    gate: GateConfig = field(default_factory=GateConfig)


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path or os.environ.get("SCRAPER_CONFIG") or DEFAULT_PATH)
    raw: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
    return Config(
        ladder=list(raw.get("ladder", ["plain"])),
        plain=PlainConfig(**raw.get("plain", {})),
        paid=PaidConfig(
            **raw.get("paid", {}), api_key=os.environ.get("FIRECRAWL_API_KEY") or None
        ),
        gate=GateConfig(**raw.get("gate", {})),
    )
