from datetime import datetime, timedelta, timezone

import pytest

from tradingagents.assets import AssetRequest, MarketType, resolve_asset
from tradingagents.evidence import (
    CryptoEvidencePacket,
    Observation,
    ProviderError,
)


BTC = resolve_asset(AssetRequest(symbol="BTC-USD", asset_type="crypto"))
NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


def test_observation_requires_utc_timestamps():
    with pytest.raises(ValueError, match="observed_at must be timezone-aware UTC"):
        Observation(
            provider="coingecko",
            canonical_asset_id=BTC.canonical_id,
            data_type="market",
            observed_at=datetime(2026, 8, 15, 12, 0),
            payload={"price": 100_000},
        )


def test_observation_is_immutable_and_serializes_provenance():
    observation = Observation(
        provider="coingecko",
        canonical_asset_id=BTC.canonical_id,
        data_type="market",
        observed_at=NOW,
        source_timestamp=NOW - timedelta(seconds=5),
        quote_currency="USD",
        market_type=MarketType.SPOT,
        payload={"price": 100_000},
        provenance={"endpoint": "/coins/bitcoin"},
        stale=False,
        warnings=("sample warning",),
    )

    assert observation.payload["price"] == 100_000
    with pytest.raises(TypeError):
        observation.payload["price"] = 1
    assert observation.to_dict() == {
        "provider": "coingecko",
        "canonical_asset_id": BTC.canonical_id,
        "data_type": "market",
        "observed_at": "2026-08-15T12:00:00+00:00",
        "source_timestamp": "2026-08-15T11:59:55+00:00",
        "quote_currency": "USD",
        "market_type": "spot",
        "payload": {"price": 100_000},
        "provenance": {"endpoint": "/coins/bitcoin"},
        "stale": False,
        "warnings": ["sample warning"],
    }


def test_provider_error_is_sanitized_and_secret_free():
    error = ProviderError.sanitized(
        provider="coingecko",
        code="timeout",
        message="GET failed Authorization: Bearer secret-token?x_cg_demo_api_key=also-secret",
        retryable=True,
        observed_at=NOW,
    )

    assert "secret-token" not in error.message
    assert "also-secret" not in error.message
    assert "[REDACTED]" in error.message
    assert error.to_dict()["retryable"] is True


def test_crypto_evidence_packet_groups_observations_and_errors():
    observation = Observation(
        provider="coingecko",
        canonical_asset_id=BTC.canonical_id,
        data_type="market",
        observed_at=NOW,
        market_type=MarketType.SPOT,
        payload={"price": 100_000},
    )
    error = ProviderError.sanitized(
        provider="geckoterminal",
        code="timeout",
        message="request timed out",
        observed_at=NOW,
    )
    packet = CryptoEvidencePacket(
        asset=BTC,
        observations=(observation,),
        provider_errors=(error,),
        generated_at=NOW,
        completeness=0.5,
        freshness_summary={"market": "fresh"},
        conflicts=("reference spot unavailable",),
    )

    assert packet.grouped_observations == {"market": (observation,)}
    assert packet.to_dict()["asset"]["canonical_id"] == BTC.canonical_id
    assert packet.to_dict()["provider_errors"][0]["provider"] == "geckoterminal"


def test_packet_rejects_observation_for_another_asset():
    observation = Observation(
        provider="coingecko",
        canonical_asset_id="crypto:coingecko:ethereum:spot:USD",
        data_type="market",
        observed_at=NOW,
        payload={"price": 4_000},
    )
    with pytest.raises(ValueError, match="does not match packet asset"):
        CryptoEvidencePacket(asset=BTC, observations=(observation,), generated_at=NOW)
