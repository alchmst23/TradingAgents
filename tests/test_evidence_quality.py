from datetime import datetime, timedelta, timezone

from tradingagents.evidence import Observation
from tradingagents.evidence.quality import (
    FreshnessPolicy,
    detect_price_conflicts,
    evaluate_freshness,
)


NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


def observation(provider, price, age_seconds, data_type="market"):
    timestamp = NOW - timedelta(seconds=age_seconds)
    return Observation(
        provider=provider,
        canonical_asset_id="crypto:coingecko:bitcoin:spot:USD",
        data_type=data_type,
        observed_at=NOW,
        source_timestamp=timestamp,
        quote_currency="USD",
        market_type="spot",
        payload={"price": price},
    )


def test_freshness_policy_is_data_type_specific():
    policy = FreshnessPolicy(
        max_age_by_type={"market": timedelta(minutes=2), "metadata": timedelta(days=1)}
    )
    assert evaluate_freshness(observation("a", 100, 60), NOW, policy).stale is False
    assert evaluate_freshness(observation("a", 100, 121), NOW, policy).stale is True


def test_missing_source_timestamp_is_stale_and_warned():
    item = Observation(
        provider="coingecko",
        canonical_asset_id="crypto:coingecko:bitcoin:spot:USD",
        data_type="market",
        observed_at=NOW,
        payload={"price": 100},
    )
    evaluated = evaluate_freshness(item, NOW, FreshnessPolicy.default())
    assert evaluated.stale is True
    assert "missing source timestamp" in evaluated.warnings


def test_price_conflicts_are_explicit_and_quote_scoped():
    observations = (
        observation("coingecko", 100.0, 10),
        observation("geckoterminal", 104.0, 15),
        observation("other", 100.2, 10),
    )
    conflicts = detect_price_conflicts(observations, relative_threshold=0.03)
    assert len(conflicts) == 2
    assert conflicts[0].providers == ("coingecko", "geckoterminal")
    assert all(conflict.relative_difference > 0.03 for conflict in conflicts)


def test_stale_and_nonpositive_prices_do_not_create_conflicts():
    stale = evaluate_freshness(observation("old", 150, 10_000), NOW, FreshnessPolicy.default())
    fresh = observation("fresh", 100, 10)
    nonpositive = observation("bad", 0, 10)
    assert detect_price_conflicts((stale, fresh, nonpositive)) == ()
