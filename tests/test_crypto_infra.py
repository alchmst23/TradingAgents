from datetime import datetime, timedelta, timezone

import pytest

from tradingagents.dataflows.crypto_infra import BoundedTTLCache, HTTPPolicy


class Clock:
    def __init__(self):
        self.now = datetime(2026, 8, 15, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def test_http_policy_allows_only_configured_https_hosts():
    policy = HTTPPolicy(allowed_hosts=frozenset({"api.coingecko.com"}))
    assert policy.validate_url("https://api.coingecko.com/api/v3/ping") is None
    with pytest.raises(ValueError, match="HTTPS"):
        policy.validate_url("http://api.coingecko.com/api/v3/ping")
    with pytest.raises(ValueError, match="allowlisted"):
        policy.validate_url("https://evil.example/api.coingecko.com")
    with pytest.raises(ValueError, match="credentials"):
        policy.validate_url("https://user:password@api.coingecko.com/api/v3/ping")


def test_http_policy_has_bounded_defaults():
    policy = HTTPPolicy(allowed_hosts=frozenset({"api.coingecko.com"}))
    assert policy.connect_timeout_seconds <= 10
    assert policy.read_timeout_seconds <= 30
    assert 0 <= policy.max_retries <= 3
    assert policy.max_response_bytes <= 5_000_000


def test_ttl_cache_expires_and_is_bounded_lru():
    clock = Clock()
    cache = BoundedTTLCache(max_entries=2, ttl=timedelta(seconds=10), clock=clock)
    cache.set("a", 1)
    cache.set("b", 2)
    assert cache.get("a") == 1  # refresh a, making b least recently used
    cache.set("c", 3)
    assert cache.get("b") is None
    assert cache.get("a") == 1
    clock.now += timedelta(seconds=11)
    assert cache.get("a") is None
    assert cache.get("c") is None


def test_ttl_cache_rejects_unbounded_configuration():
    with pytest.raises(ValueError, match="max_entries"):
        BoundedTTLCache(max_entries=0)
    with pytest.raises(ValueError, match="ttl"):
        BoundedTTLCache(max_entries=1, ttl=timedelta(0))
