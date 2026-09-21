"""Runtime configuration, read once from the environment. No secrets are hardcoded."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    return default if val is None else val.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    kap_api_key: str
    kap_api_secret: str | None
    kap_test_mode: bool
    kap_timeout: float
    kap_cache_ttl: float
    kap_max_concurrency: int
    yahoo_timeout: float
    # Hard ceilings that keep one tool call from hammering KAP or flooding the model.
    max_scan_pages: int
    max_result_chars: int
    log_level: str

    @property
    def kap_configured(self) -> bool:
        return bool(self.kap_api_key)


def load_settings() -> Settings:
    return Settings(
        kap_api_key=os.environ.get("KAP_API_KEY", "").strip(),
        kap_api_secret=os.environ.get("KAP_API_SECRET") or None,
        kap_test_mode=_bool("KAP_TEST_MODE", False),
        kap_timeout=_float("KAP_TIMEOUT", 30.0),
        kap_cache_ttl=_float("KAP_CACHE_TTL", 600.0),
        kap_max_concurrency=_int("KAP_MAX_CONCURRENCY", 4),
        yahoo_timeout=_float("YAHOO_TIMEOUT", 45.0),
        max_scan_pages=_int("KAP_MAX_SCAN_PAGES", 400),
        max_result_chars=_int("KAP_MAX_RESULT_CHARS", 60000),
        log_level=os.environ.get("KAP_LOG_LEVEL", "INFO").upper(),
    )
