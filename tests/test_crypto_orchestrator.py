from datetime import datetime, timedelta, timezone

from tradingagents.assets import AssetRequest, resolve_asset
from tradingagents.dataflows.crypto_orchestrator import CryptoEvidenceOrchestrator
from tradingagents.evidence import Observation, ProviderError


NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
BTC = resolve_asset(AssetRequest(symbol="BTC-USD", asset_type="crypto"))
CULT = resolve_asset(
    AssetRequest(
        symbol="CULT",
        asset_type="crypto",
        chain="base",
        contract_address="0x0c03ce270b4826ec62e7dd007f0b716068639f7b",
    )
)
BTC_PERP = resolve_asset(
    AssetRequest(
        symbol="BTC",
        asset_type="crypto",
        venue="hyperliquid",
        instrument="BTC",
        market_type="perpetual",
    )
)


def observation(asset, provider, data_type, *, price=None, age_seconds=10):
    payload = {} if price is None else {"price": price}
    return Observation(
        provider=provider,
        canonical_asset_id=asset.canonical_id,
        data_type=data_type,
        observed_at=NOW,
        source_timestamp=NOW - timedelta(seconds=age_seconds),
        quote_currency=asset.quote_currency,
        market_type=asset.market_type,
        payload=payload,
    )


class StubAdapter:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def fetch(self, asset):
        self.calls.append(asset)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def orchestrator(*, coingecko=None, geckoterminal=None, hyperliquid=None):
    return CryptoEvidenceOrchestrator(
        coingecko=coingecko,
        geckoterminal=geckoterminal,
        hyperliquid=hyperliquid,
        clock=lambda: NOW,
    )


def test_contract_spot_routes_to_coingecko_and_geckoterminal_and_aggregates_quality():
    cg = StubAdapter(
        (
            (
                observation(CULT, "coingecko", "metadata"),
                observation(CULT, "coingecko", "market", price=1.00),
            ),
            (),
        )
    )
    gt = StubAdapter(((observation(CULT, "geckoterminal", "dex", price=1.10),), ()))
    hl = StubAdapter(AssertionError("perpetual adapter must not be called"))

    packet = orchestrator(coingecko=cg, geckoterminal=gt, hyperliquid=hl).collect(CULT)

    assert [item.data_type for item in packet.observations] == ["metadata", "market", "dex"]
    assert packet.completeness == 1.0
    assert packet.freshness_summary == {
        "dex": "fresh",
        "market": "fresh",
        "metadata": "fresh",
    }
    assert len(packet.conflicts) == 1
    assert "coingecko/geckoterminal" in packet.conflicts[0]
    assert cg.calls == [CULT]
    assert gt.calls == [CULT]
    assert hl.calls == []


def test_coingecko_spot_does_not_call_contract_or_perpetual_adapters():
    cg = StubAdapter(
        (((observation(BTC, "coingecko", "metadata"), observation(BTC, "coingecko", "market", price=60_000))), ())
    )
    gt = StubAdapter(AssertionError("DEX adapter must not be called"))
    hl = StubAdapter(AssertionError("perpetual adapter must not be called"))

    packet = orchestrator(coingecko=cg, geckoterminal=gt, hyperliquid=hl).collect(BTC)

    assert packet.completeness == 1.0
    assert gt.calls == []
    assert hl.calls == []


def test_hyperliquid_perpetual_routes_only_to_derivatives_adapter():
    hl = StubAdapter(
        (
            (
                observation(BTC_PERP, "hyperliquid", "derivatives_market", price=60_000),
                observation(BTC_PERP, "hyperliquid", "funding_history"),
                observation(BTC_PERP, "hyperliquid", "order_book"),
            ),
            (),
        )
    )
    cg = StubAdapter(AssertionError("spot adapter must not be called"))
    gt = StubAdapter(AssertionError("DEX adapter must not be called"))

    packet = orchestrator(coingecko=cg, geckoterminal=gt, hyperliquid=hl).collect(BTC_PERP)

    assert packet.completeness == 1.0
    assert [item.data_type for item in packet.observations] == [
        "derivatives_market",
        "funding_history",
        "order_book",
    ]
    assert cg.calls == []
    assert gt.calls == []


def test_partial_provider_failure_preserves_good_evidence_and_reduces_completeness():
    cg = StubAdapter(
        (
            (observation(CULT, "coingecko", "market", price=1.00),),
            (
                ProviderError(
                    provider="coingecko",
                    code="metadata_unavailable",
                    message="metadata unavailable",
                    observed_at=NOW,
                    data_type="metadata",
                ),
            ),
        )
    )
    gt = StubAdapter(TimeoutError("authorization=super-secret"))

    packet = orchestrator(coingecko=cg, geckoterminal=gt).collect(CULT)

    assert [item.data_type for item in packet.observations] == ["market"]
    assert packet.completeness == 1 / 3
    assert {error.provider for error in packet.provider_errors} == {"coingecko", "geckoterminal"}
    generated = next(error for error in packet.provider_errors if error.provider == "geckoterminal")
    assert generated.code == "provider_unavailable"
    assert "super-secret" not in generated.message
    assert "[REDACTED]" in generated.message
    assert packet.freshness_summary["dex"] == "missing"
    assert packet.freshness_summary["metadata"] == "missing"


def test_stale_observations_are_replaced_in_packet_without_mutating_adapter_result():
    old = observation(BTC, "coingecko", "market", price=60_000, age_seconds=301)
    cg = StubAdapter(((observation(BTC, "coingecko", "metadata"), old), ()))

    packet = orchestrator(coingecko=cg).collect(BTC)

    market = next(item for item in packet.observations if item.data_type == "market")
    assert market.stale is True
    assert packet.freshness_summary["market"] == "stale"
    assert old.stale is False


def test_quality_clock_is_sampled_after_provider_calls():
    after_fetch = NOW + timedelta(seconds=2)
    market = Observation(
        provider="coingecko",
        canonical_asset_id=BTC.canonical_id,
        data_type="market",
        observed_at=NOW + timedelta(seconds=1),
        source_timestamp=NOW + timedelta(seconds=1),
        quote_currency="USD",
        market_type=BTC.market_type,
        payload={"price": 60_000},
    )
    metadata = observation(BTC, "coingecko", "metadata")
    times = iter((NOW, after_fetch))
    service = CryptoEvidenceOrchestrator(
        coingecko=StubAdapter(((metadata, market), ())),
        clock=lambda: next(times),
    )

    packet = service.collect(BTC)

    assert packet.generated_at == after_fetch
    assert next(item for item in packet.observations if item.data_type == "market").stale is False



def test_optional_sentiment_provider_is_routed_for_spot_and_counted_in_completeness():
    cg = StubAdapter(((observation(BTC, "coingecko", "metadata"), observation(BTC, "coingecko", "market", price=60_000)), ()))
    sentiment = StubAdapter(((observation(BTC, "donna_x", "sentiment"),), ()))
    service = CryptoEvidenceOrchestrator(coingecko=cg, sentiment=sentiment, clock=lambda: NOW)

    packet = service.collect(BTC)

    assert sentiment.calls == [BTC]
    assert packet.completeness == 1.0
    assert packet.freshness_summary["sentiment"] == "fresh"


def test_sentiment_failure_degrades_without_losing_market_evidence():
    cg = StubAdapter(((observation(BTC, "coingecko", "metadata"), observation(BTC, "coingecko", "market", price=60_000)), ()))
    sentiment = StubAdapter(TimeoutError("credential=secret"))
    service = CryptoEvidenceOrchestrator(coingecko=cg, sentiment=sentiment, clock=lambda: NOW)

    packet = service.collect(BTC)

    assert {item.data_type for item in packet.observations} == {"metadata", "market"}
    assert packet.completeness == 2 / 3
    assert packet.freshness_summary["sentiment"] == "missing"
    error = next(item for item in packet.provider_errors if item.provider == "donna_x")
    assert "secret" not in error.message
