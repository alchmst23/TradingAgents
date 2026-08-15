"""CoinGecko public metadata and spot-market evidence adapter."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import requests

from tradingagents.assets import ResolvedAsset
from tradingagents.evidence import Observation, ProviderError

from .crypto_infra import BoundedTTLCache, HTTPPolicy

_BASE_URL = "https://api.coingecko.com"
_PLATFORM_IDS = {
    "ethereum": "ethereum",
    "base": "base",
    "arbitrum": "arbitrum-one",
    "optimism": "optimistic-ethereum",
    "polygon": "polygon-pos",
    "bsc": "binance-smart-chain",
    "avalanche": "avalanche",
}
_SECRET = re.compile(r"(?i)(api[_-]?key|token|secret|authorization)=([^\s&]+)")


def _utc(value: str | None, fallback: datetime) -> datetime:
    if not value:
        return fallback
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


def _sanitized_exception(exc: Exception) -> str:
    message = _SECRET.sub(r"\1=[REDACTED]", str(exc))
    return message[:240] or type(exc).__name__


class CoinGeckoAdapter:
    """Fetch and normalize public CoinGecko evidence without guessing identity."""

    def __init__(
        self,
        *,
        session: Any | None = None,
        policy: HTTPPolicy | None = None,
        cache: BoundedTTLCache | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session = session or requests.Session()
        self.policy = policy or HTTPPolicy(("api.coingecko.com",))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self.cache = cache or BoundedTTLCache(
            max_entries=128,
            ttl=timedelta(seconds=60),
            clock=self.clock,
        )

    def _target(self, asset: ResolvedAsset) -> tuple[str, str]:
        if asset.coingecko_id:
            item = quote(asset.coingecko_id, safe="")
            return f"{_BASE_URL}/api/v3/coins/{item}", "/api/v3/coins/{id}"
        if asset.chain and asset.contract_address:
            platform = _PLATFORM_IDS.get(asset.chain)
            if not platform:
                raise ValueError(f"unsupported CoinGecko platform: {asset.chain}")
            address = quote(asset.contract_address, safe="")
            return (
                f"{_BASE_URL}/api/v3/coins/{platform}/contract/{address}",
                "/api/v3/coins/{platform}/contract/{address}",
            )
        raise ValueError("CoinGecko requires coingecko_id or chain+contract identity")

    def _request(self, url: str) -> Any:
        self.policy.validate_url(url)
        last_error: Exception | None = None
        for attempt in range(self.policy.max_retries + 1):
            try:
                response = self.session.get(
                    url,
                    timeout=(
                        self.policy.connect_timeout_seconds,
                        self.policy.read_timeout_seconds,
                    ),
                    allow_redirects=False,
                    headers={"Accept": "application/json", "User-Agent": "TradingAgents/crypto-analysis"},
                )
                if len(response.content) > self.policy.max_response_bytes:
                    raise ValueError("response exceeds configured size limit")
                if response.status_code == 429 or response.status_code >= 500:
                    raise requests.HTTPError(f"provider HTTP {response.status_code}")
                if response.status_code >= 400:
                    raise ValueError(f"provider HTTP {response.status_code}")
                return response.json()
            except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
                last_error = exc
                if attempt >= self.policy.max_retries:
                    raise
                self.sleep(self.policy.backoff_seconds * (2**attempt))
        raise last_error or RuntimeError("request failed")

    def fetch(self, asset: ResolvedAsset) -> tuple[tuple[Observation, ...], tuple[ProviderError, ...]]:
        cached = self.cache.get(asset.canonical_id)
        if cached is not None:
            return cached
        observed_at = self.clock().astimezone(timezone.utc)
        try:
            url, endpoint = self._target(asset)
            payload = self._request(url)
            result = self._normalize(asset, payload, endpoint, observed_at)
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
            result = (
                (),
                (
                    ProviderError(
                        provider="coingecko",
                        code="provider_unavailable",
                        message=_sanitized_exception(exc),
                        retryable=True,
                        observed_at=observed_at,
                    ),
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            result = (
                (),
                (
                    ProviderError(
                        provider="coingecko",
                        code="invalid_payload",
                        message=_sanitized_exception(exc),
                        retryable=False,
                        observed_at=observed_at,
                    ),
                ),
            )
        self.cache.set(asset.canonical_id, result)
        return result

    @staticmethod
    def _normalize(
        asset: ResolvedAsset,
        raw: dict[str, Any],
        endpoint: str,
        observed_at: datetime,
    ) -> tuple[tuple[Observation, ...], tuple[ProviderError, ...]]:
        market = raw["market_data"]
        price = market["current_price"]["usd"]
        if not isinstance(price, (int, float)) or price <= 0:
            raise ValueError("missing or invalid USD current price")
        source_timestamp = _utc(raw.get("last_updated"), observed_at)
        provenance = {"endpoint": endpoint, "provider_asset_id": raw["id"]}
        metadata = Observation(
            provider="coingecko",
            data_type="metadata",
            canonical_asset_id=asset.canonical_id,
            observed_at=observed_at,
            source_timestamp=source_timestamp,
            quote_currency="USD",
            payload={
                "coingecko_id": raw["id"],
                "symbol": raw.get("symbol"),
                "name": raw.get("name"),
                "asset_platform_id": raw.get("asset_platform_id"),
                "contract_address": raw.get("contract_address"),
                "categories": raw.get("categories") or [],
                "description": (raw.get("description") or {}).get("en"),
                "homepage": ((raw.get("links") or {}).get("homepage") or [None])[0],
            },
            provenance=provenance,
        )
        market_observation = Observation(
            provider="coingecko",
            data_type="market",
            canonical_asset_id=asset.canonical_id,
            observed_at=observed_at,
            source_timestamp=source_timestamp,
            quote_currency="USD",
            payload={
                "price": float(price),
                "market_cap": market.get("market_cap", {}).get("usd"),
                "volume_24h": market.get("total_volume", {}).get("usd"),
                "price_change_percentage_24h": market.get("price_change_percentage_24h"),
                "circulating_supply": market.get("circulating_supply"),
                "total_supply": market.get("total_supply"),
            },
            provenance=provenance,
        )
        return (metadata, market_observation), ()
