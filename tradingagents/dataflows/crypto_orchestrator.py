"""Identity-aware orchestration for normalized crypto evidence providers."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol

from tradingagents.assets import MarketType, ResolvedAsset
from tradingagents.dataflows.coingecko import CoinGeckoAdapter
from tradingagents.dataflows.geckoterminal import GeckoTerminalAdapter
from tradingagents.dataflows.hyperliquid_derivatives import HyperliquidDerivativesAdapter
from tradingagents.evidence import CryptoEvidencePacket, Observation, ProviderError
from tradingagents.evidence.quality import (
    FreshnessPolicy,
    detect_price_conflicts,
    evaluate_freshness,
)


class EvidenceAdapter(Protocol):
    def fetch(
        self, asset: ResolvedAsset
    ) -> tuple[tuple[Observation, ...], tuple[ProviderError, ...]]: ...


class CryptoEvidenceOrchestrator:
    """Select relevant providers and assemble one quality-evaluated packet."""

    def __init__(
        self,
        *,
        coingecko: EvidenceAdapter | None = None,
        geckoterminal: EvidenceAdapter | None = None,
        hyperliquid: EvidenceAdapter | None = None,
        clock: Callable[[], datetime] | None = None,
        freshness_policy: FreshnessPolicy | None = None,
        price_conflict_threshold: float = 0.05,
    ) -> None:
        if price_conflict_threshold < 0:
            raise ValueError("price_conflict_threshold must be non-negative")
        self.coingecko = coingecko or CoinGeckoAdapter()
        self.geckoterminal = geckoterminal or GeckoTerminalAdapter()
        self.hyperliquid = hyperliquid or HyperliquidDerivativesAdapter()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.freshness_policy = freshness_policy or FreshnessPolicy.default()
        self.price_conflict_threshold = price_conflict_threshold

    def collect(self, asset: ResolvedAsset) -> CryptoEvidencePacket:
        started_at = self.clock()
        observations: list[Observation] = []
        errors: list[ProviderError] = []
        providers, expected_types = self._route(asset)

        for provider_name, adapter in providers:
            try:
                provider_observations, provider_errors = adapter.fetch(asset)
                observations.extend(provider_observations)
                errors.extend(provider_errors)
            except Exception as exc:  # Provider isolation is intentional.
                errors.append(
                    ProviderError.sanitized(
                        provider=provider_name,
                        code="provider_unavailable",
                        message=(
                            f"{provider_name} adapter failed with "
                            f"{type(exc).__name__}; details [REDACTED]"
                        ),
                        observed_at=started_at,
                        retryable=isinstance(exc, (TimeoutError, ConnectionError)),
                    )
                )

        generated_at = self.clock()
        evaluated = tuple(
            evaluate_freshness(item, generated_at, self.freshness_policy)
            for item in observations
        )
        conflicts = detect_price_conflicts(
            evaluated,
            relative_threshold=self.price_conflict_threshold,
        )
        present_types = {item.data_type for item in evaluated}
        completeness = (
            len(present_types.intersection(expected_types)) / len(expected_types)
            if expected_types
            else 0.0
        )

        return CryptoEvidencePacket(
            asset=asset,
            observations=evaluated,
            provider_errors=tuple(errors),
            generated_at=generated_at,
            completeness=completeness,
            freshness_summary=self._freshness_summary(evaluated, expected_types),
            conflicts=tuple(self._format_conflict(item) for item in conflicts),
        )

    def _route(
        self, asset: ResolvedAsset
    ) -> tuple[tuple[tuple[str, EvidenceAdapter], ...], frozenset[str]]:
        if (
            asset.venue == "hyperliquid"
            and asset.market_type == MarketType.PERPETUAL
        ):
            return (
                (("hyperliquid", self.hyperliquid),),
                frozenset({"derivatives_market", "funding_history", "order_book"}),
            )

        providers: list[tuple[str, EvidenceAdapter]] = [
            ("coingecko", self.coingecko)
        ]
        expected = {"metadata", "market"}
        if asset.chain and asset.contract_address:
            providers.append(("geckoterminal", self.geckoterminal))
            expected.add("dex")
        return tuple(providers), frozenset(expected)

    @staticmethod
    def _freshness_summary(
        observations: tuple[Observation, ...], expected_types: frozenset[str]
    ) -> dict[str, str]:
        summary: dict[str, str] = {}
        for data_type in sorted(expected_types):
            matching = [item for item in observations if item.data_type == data_type]
            if not matching:
                summary[data_type] = "missing"
            elif all(item.stale for item in matching):
                summary[data_type] = "stale"
            elif any(item.stale for item in matching):
                summary[data_type] = "mixed"
            else:
                summary[data_type] = "fresh"
        return summary

    @staticmethod
    def _format_conflict(conflict) -> str:
        left, right = conflict.providers
        quote = conflict.quote_currency or "unknown quote"
        difference = conflict.relative_difference * 100
        return (
            f"price conflict {left}/{right}: {difference:.2f}% difference "
            f"in {quote} ({conflict.prices[0]:g} vs {conflict.prices[1]:g})"
        )
