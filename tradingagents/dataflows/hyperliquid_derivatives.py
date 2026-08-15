"""Normalized analysis-only Hyperliquid perpetual derivatives evidence."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from tradingagents.assets import MarketType, ResolvedAsset
from tradingagents.evidence import Observation, ProviderError

from .crypto_infra import BoundedTTLCache, HTTPPolicy

API_URL = "https://api.hyperliquid.xyz/info"
_PROVIDER = "hyperliquid"

_SECRET_PATTERN = re.compile(
    r"(?i)(authorization|api[_-]?key|token|secret|signature)\s*[=:]\s*[^\s,;]+"
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _timestamp_ms(value: Any) -> datetime | None:
    number = _number(value)
    if number is None:
        return None
    try:
        return datetime.fromtimestamp(number / 1000, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _sanitized_message(exc: Exception) -> str:
    message = _SECRET_PATTERN.sub(r"\1=[REDACTED]", str(exc))
    return message[:240] or type(exc).__name__


class HyperliquidDerivativesAdapter:
    """Fetch bounded market state, funding history, and L2 book evidence."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        policy: HTTPPolicy | None = None,
        cache: BoundedTTLCache | None = None,
        clock: Callable[[], datetime] = _utc_now,
        sleeper: Callable[[float], None] = time.sleep,
        max_book_levels: int = 20,
        funding_lookback: timedelta = timedelta(hours=24),
    ) -> None:
        if not 1 <= max_book_levels <= 50:
            raise ValueError("max_book_levels must be between 1 and 50")
        self.session = session or requests.Session()
        self.policy = policy or HTTPPolicy(("api.hyperliquid.xyz",))
        self.clock = clock
        self.sleeper = sleeper
        self.max_book_levels = max_book_levels
        self.funding_lookback = funding_lookback
        self.cache = cache or BoundedTTLCache(
            max_entries=128,
            ttl=timedelta(seconds=15),
            clock=self.clock,
        )

    def fetch(
        self, asset: ResolvedAsset
    ) -> tuple[tuple[Observation, ...], tuple[ProviderError, ...]]:
        observed_at = self.clock()
        if (
            asset.venue != "hyperliquid"
            or asset.market_type is not MarketType.PERPETUAL
            or not asset.instrument
        ):
            return (), (
                ProviderError(
                    provider=_PROVIDER,
                    code="unsupported_identity",
                    message="Hyperliquid derivatives require an explicit perpetual venue identity",
                    observed_at=observed_at,
                    retryable=False,
                ),
            )

        coin = asset.instrument
        cache_key = (_PROVIDER, asset.canonical_id, "derivatives")
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            meta_payload = self._post({"type": "metaAndAssetCtxs"})
            market = self._market_observation(asset, coin, meta_payload, observed_at)
            if market is None:
                return (), (
                    ProviderError(
                        provider=_PROVIDER,
                        code="instrument_not_found",
                        message="Requested Hyperliquid instrument was not listed",
                        observed_at=observed_at,
                        retryable=False,
                        data_type="derivatives_market",
                    ),
                )

            start_ms = int((observed_at - self.funding_lookback).timestamp() * 1000)
            funding_payload = self._post(
                {"type": "fundingHistory", "coin": coin, "startTime": start_ms}
            )
            book_payload = self._post({"type": "l2Book", "coin": coin})
            funding = self._funding_observation(
                asset, coin, funding_payload, observed_at
            )
            book = self._book_observation(asset, coin, book_payload, observed_at)
            result = (tuple(item for item in (market, funding, book) if item), ())
            self.cache.set(cache_key, result)
            return result
        except Exception as exc:  # provider boundary must degrade safely
            retryable = isinstance(exc, (requests.Timeout, requests.ConnectionError))
            return (), (
                ProviderError(
                    provider=_PROVIDER,
                    code="provider_unavailable",
                    message=_sanitized_message(exc),
                    observed_at=observed_at,
                    retryable=retryable,
                ),
            )

    def _post(self, payload: dict[str, Any]) -> Any:
        self.policy.validate_url(API_URL)
        attempts = self.policy.max_retries + 1
        for attempt in range(attempts):
            try:
                response = self.session.post(
                    API_URL,
                    json=payload,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "User-Agent": "TradingAgents/crypto-analysis",
                    },
                    timeout=(
                        self.policy.connect_timeout_seconds,
                        self.policy.read_timeout_seconds,
                    ),
                    allow_redirects=False,
                )
                if len(response.content) > self.policy.max_response_bytes:
                    raise ValueError("provider response exceeded size limit")
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    if attempt + 1 < attempts:
                        self.sleeper(self.policy.backoff_seconds * (2**attempt))
                        continue
                    raise requests.HTTPError(
                        f"Hyperliquid returned HTTP {response.status_code}"
                    )
                if response.status_code >= 400:
                    raise ValueError(
                        f"Hyperliquid request rejected with HTTP {response.status_code}"
                    )
                return response.json()
            except (requests.Timeout, requests.ConnectionError):
                if attempt + 1 >= attempts:
                    raise
                self.sleeper(self.policy.backoff_seconds * (2**attempt))
        raise RuntimeError("unreachable retry state")

    def _market_observation(
        self,
        asset: ResolvedAsset,
        coin: str,
        raw: Any,
        observed_at: datetime,
    ) -> Observation | None:
        if not isinstance(raw, list) or len(raw) != 2:
            raise ValueError("invalid Hyperliquid market response")
        meta, contexts = raw
        universe = meta.get("universe") if isinstance(meta, dict) else None
        if not isinstance(universe, list) or not isinstance(contexts, list):
            raise ValueError("invalid Hyperliquid market schema")
        index = next(
            (i for i, item in enumerate(universe) if item.get("name") == coin),
            None,
        )
        if index is None or index >= len(contexts):
            return None
        specification = universe[index]
        context = contexts[index]
        mark = _number(context.get("markPx"))
        oracle = _number(context.get("oraclePx"))
        open_interest = _number(context.get("openInterest"))
        payload = {
            "instrument": coin,
            "mark_price": mark,
            "oracle_price": oracle,
            "mid_price": _number(context.get("midPx")),
            "funding_rate": _number(context.get("funding")),
            "premium": _number(context.get("premium")),
            "open_interest_base": open_interest,
            "open_interest_usd": (
                open_interest * mark
                if open_interest is not None and mark is not None
                else None
            ),
            "day_notional_volume_usd": _number(context.get("dayNtlVlm")),
            "previous_day_price": _number(context.get("prevDayPx")),
            "impact_bid_price": _number((context.get("impactPxs") or [None])[0]),
            "impact_ask_price": _number((context.get("impactPxs") or [None, None])[1]),
            "basis_to_oracle_percentage": (
                (mark / oracle - 1) * 100
                if mark is not None and oracle not in (None, 0)
                else None
            ),
            "size_decimals": specification.get("szDecimals"),
            "max_leverage": specification.get("maxLeverage"),
            "isolated_only": bool(specification.get("onlyIsolated", False)),
        }
        return self._observation(
            asset, "derivatives_market", observed_at, payload, observed_at
        )

    def _funding_observation(
        self,
        asset: ResolvedAsset,
        coin: str,
        raw: Any,
        observed_at: datetime,
    ) -> Observation:
        if not isinstance(raw, list):
            raise ValueError("invalid Hyperliquid funding response")
        records = []
        timestamps = []
        for item in raw[-500:]:
            if not isinstance(item, dict) or item.get("coin") != coin:
                continue
            timestamp = _timestamp_ms(item.get("time"))
            if timestamp:
                timestamps.append(timestamp)
            records.append(
                {
                    "timestamp": timestamp.isoformat() if timestamp else None,
                    "funding_rate": _number(item.get("fundingRate")),
                    "premium": _number(item.get("premium")),
                }
            )
        payload = {
            "instrument": coin,
            "records": tuple(records),
            "funding_interval": "1h",
            "rate_units": "fraction_per_interval",
        }
        return self._observation(
            asset,
            "funding_history",
            observed_at,
            payload,
            max(timestamps) if timestamps else None,
        )

    def _book_observation(
        self,
        asset: ResolvedAsset,
        coin: str,
        raw: Any,
        observed_at: datetime,
    ) -> Observation:
        if not isinstance(raw, dict) or not isinstance(raw.get("levels"), list):
            raise ValueError("invalid Hyperliquid order-book response")
        levels = raw["levels"]
        if len(levels) != 2:
            raise ValueError("invalid Hyperliquid order-book levels")

        def normalize(items: Any) -> tuple[dict[str, Any], ...]:
            if not isinstance(items, list):
                return ()
            return tuple(
                {
                    "price": _number(item.get("px")),
                    "size": _number(item.get("sz")),
                    "orders": item.get("n"),
                }
                for item in items[: self.max_book_levels]
                if isinstance(item, dict)
            )

        bids = normalize(levels[0])
        asks = normalize(levels[1])
        best_bid = bids[0]["price"] if bids else None
        best_ask = asks[0]["price"] if asks else None
        spread = (
            best_ask - best_bid
            if best_bid is not None and best_ask is not None
            else None
        )
        midpoint = (
            (best_bid + best_ask) / 2
            if best_bid is not None and best_ask is not None
            else None
        )
        payload = {
            "instrument": coin,
            "bids": bids,
            "asks": asks,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "spread_bps": (
                spread / midpoint * 10_000
                if spread is not None and midpoint not in (None, 0)
                else None
            ),
            "depth_levels_limit": self.max_book_levels,
        }
        return self._observation(
            asset,
            "order_book",
            observed_at,
            payload,
            _timestamp_ms(raw.get("time")),
        )

    @staticmethod
    def _observation(
        asset: ResolvedAsset,
        data_type: str,
        observed_at: datetime,
        payload: dict[str, Any],
        source_timestamp: datetime | None,
    ) -> Observation:
        return Observation(
            provider=_PROVIDER,
            canonical_asset_id=asset.canonical_id,
            data_type=data_type,
            observed_at=observed_at,
            source_timestamp=source_timestamp,
            quote_currency=asset.quote_currency,
            market_type=asset.market_type,
            payload=payload,
            provenance={
                "source_url": API_URL,
                "attribution": "Hyperliquid public Info API",
            },
        )
