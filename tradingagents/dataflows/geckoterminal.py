"""GeckoTerminal contract-scoped DEX liquidity evidence adapter."""

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

_BASE_URL = "https://api.geckoterminal.com/api/v2"
_NETWORKS = {
    "base": "base",
    "ethereum": "eth",
    "arbitrum": "arbitrum",
    "optimism": "optimism",
    "polygon": "polygon_pos",
    "bsc": "bsc",
    "avalanche": "avax",
}
_SECRET = re.compile(r"(?i)(api[_-]?key|token|authorization|secret)=([^\s&]+)")


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _sanitized(exc: Exception) -> str:
    return _SECRET.sub(r"\1=[REDACTED]", str(exc))[:240] or type(exc).__name__


def _relationship_id(pool: dict[str, Any], name: str) -> str | None:
    try:
        value = pool["relationships"][name]["data"]["id"]
    except (KeyError, TypeError):
        return None
    return str(value).lower()


class GeckoTerminalAdapter:
    """Fetch the most liquid pool explicitly linked to a chain+contract identity."""

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
        self.policy = policy or HTTPPolicy(("api.geckoterminal.com",))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self.cache = cache or BoundedTTLCache(
            max_entries=128,
            ttl=timedelta(seconds=45),
            clock=self.clock,
        )

    def fetch(self, asset: ResolvedAsset) -> tuple[tuple[Observation, ...], tuple[ProviderError, ...]]:
        now = self.clock()
        if not asset.chain or not asset.contract_address:
            return (), (ProviderError(
                provider="geckoterminal",
                code="unsupported_identity",
                message="GeckoTerminal requires explicit chain and contract identity",
                retryable=False,
                observed_at=now,
            ),)
        network = asset.geckoterminal_network or _NETWORKS.get(asset.chain)
        if not network:
            return (), (ProviderError(
                provider="geckoterminal",
                code="unsupported_network",
                message=f"Unsupported GeckoTerminal network: {asset.chain}",
                retryable=False,
                observed_at=now,
            ),)
        key = (network, asset.contract_address.lower())
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        url = (
            f"{_BASE_URL}/networks/{quote(network, safe='')}/tokens/"
            f"{quote(asset.contract_address, safe='')}/pools?page=1"
        )
        self.policy.validate_url(url)
        try:
            response = self._request(url)
            result = self._normalize(asset, network, url, response.json(), now)
        except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
            result = (), (ProviderError(
                provider="geckoterminal",
                code="provider_unavailable",
                message=_sanitized(exc),
                retryable=isinstance(exc, requests.RequestException),
                observed_at=now,
            ),)
        if result[0]:
            self.cache.set(key, result)
        return result

    def _request(self, url: str) -> Any:
        last: Exception | None = None
        for attempt in range(self.policy.max_retries + 1):
            try:
                response = self.session.get(
                    url,
                    timeout=(self.policy.connect_timeout_seconds, self.policy.read_timeout_seconds),
                    allow_redirects=False,
                    headers={"Accept": "application/json", "User-Agent": "TradingAgents/crypto-analysis"},
                )
                if response.status_code >= 400:
                    raise requests.HTTPError(f"GeckoTerminal HTTP {response.status_code}")
                if len(response.content) > self.policy.max_response_bytes:
                    raise ValueError("GeckoTerminal response exceeds configured size limit")
                return response
            except requests.RequestException as exc:
                last = exc
                if attempt < self.policy.max_retries:
                    self.sleep(self.policy.backoff_seconds * (2**attempt))
        assert last is not None
        raise last

    @staticmethod
    def _normalize(
        asset: ResolvedAsset,
        network: str,
        url: str,
        body: dict[str, Any],
        observed_at: datetime,
    ) -> tuple[tuple[Observation, ...], tuple[ProviderError, ...]]:
        pools = body.get("data")
        if not isinstance(pools, list):
            raise ValueError("GeckoTerminal payload missing pool data")
        token_id = f"{network}_{asset.contract_address}".lower()
        verified = [
            pool for pool in pools
            if isinstance(pool, dict)
            and token_id in {
                _relationship_id(pool, "base_token"),
                _relationship_id(pool, "quote_token"),
            }
        ]
        if not verified:
            return (), (ProviderError(
                provider="geckoterminal",
                code="identity_mismatch",
                message="No returned pool is linked to the requested contract",
                retryable=False,
                observed_at=observed_at,
            ),)
        pool = max(
            verified,
            key=lambda item: _number(item.get("attributes", {}).get("reserve_in_usd")) or -1.0,
        )
        attrs = pool.get("attributes", {})
        relationships = pool.get("relationships", {})
        base_id = _relationship_id(pool, "base_token")
        token_is_base = base_id == token_id
        price_key = "base_token_price_usd" if token_is_base else "quote_token_price_usd"
        tx = attrs.get("transactions", {}).get("h24", {})
        payload = {
            "network": network,
            "pool_address": attrs.get("address") or str(pool.get("id", "")).removeprefix(f"{network}_"),
            "pool_name": attrs.get("name"),
            "dex_id": _relationship_id(pool, "dex"),
            "price_usd": _number(attrs.get(price_key)),
            "liquidity_usd": _number(attrs.get("reserve_in_usd")),
            "volume_usd_24h": _number(attrs.get("volume_usd", {}).get("h24")),
            "price_change_percentage_24h": _number(
                attrs.get("price_change_percentage", {}).get("h24")
            ),
            "transactions_24h": {
                "buys": int(tx.get("buys", 0)),
                "sells": int(tx.get("sells", 0)),
            },
            "fdv_usd": _number(attrs.get("fdv_usd")),
            "market_cap_usd": _number(attrs.get("market_cap_usd")),
            "pool_created_at": attrs.get("pool_created_at"),
            "requested_contract": asset.contract_address,
        }
        return (Observation(
            provider="geckoterminal",
            canonical_asset_id=asset.canonical_id,
            data_type="dex",
            observed_at=observed_at,
            source_timestamp=observed_at,
            quote_currency="USD",
            market_type=asset.market_type,
            payload=payload,
            provenance={
                "source_url": url,
                "attribution": "GeckoTerminal public API",
            },
        ),), ()
