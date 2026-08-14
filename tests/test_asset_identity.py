import pytest

from tradingagents.assets import (
    AmbiguousAssetError,
    AssetRequest,
    AssetType,
    AssetValidationError,
    MarketType,
    resolve_asset,
)
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.trading_graph import TradingAgentsGraph


def test_existing_stock_ticker_is_backward_compatible():
    resolved = resolve_asset(AssetRequest(symbol="AAPL"))
    assert resolved.canonical_id == "equity:AAPL"
    assert resolved.asset_type is AssetType.STOCK
    assert resolved.market_type is MarketType.EQUITY


def test_existing_bitcoin_pair_resolves_to_spot_identity():
    resolved = resolve_asset(AssetRequest(symbol="BTC-USD"))
    assert resolved.canonical_id == "crypto:coingecko:bitcoin:spot:USD"
    assert resolved.coingecko_id == "bitcoin"
    assert resolved.market_type is MarketType.SPOT
    assert resolved.quote_currency == "USD"


def test_explicit_coingecko_identity_can_be_represented():
    resolved = resolve_asset(
        AssetRequest(
            asset_type=AssetType.CRYPTO,
            symbol="BTC",
            coingecko_id="bitcoin",
            market_type=MarketType.SPOT,
            quote_currency="USD",
        )
    )
    assert resolved.provider_ids["coingecko"] == "bitcoin"
    assert resolved.resolution_confidence == 1.0


def test_hyperliquid_perpetual_is_distinct_from_spot():
    resolved = resolve_asset(
        AssetRequest(
            asset_type="crypto",
            symbol="BTC",
            venue="hyperliquid",
            instrument="BTC",
            market_type="perpetual",
            quote_currency="USDC",
        )
    )
    assert resolved.canonical_id == "crypto:hyperliquid:BTC:perpetual:USDC"
    assert resolved.market_type is MarketType.PERPETUAL
    assert resolved.provider_ids["hyperliquid"] == "BTC"


def test_chain_and_contract_identify_on_chain_token():
    address = "0x1111111111111111111111111111111111111111"
    resolved = resolve_asset(
        AssetRequest(
            asset_type="crypto",
            symbol="TOKEN",
            chain="ethereum",
            contract_address=address,
        )
    )
    assert resolved.canonical_id == f"crypto:ethereum:{address}"
    assert resolved.contract_address == address
    assert resolved.provider_ids["ethereum"] == address


def test_unknown_crypto_ticker_is_not_silently_guessed():
    with pytest.raises(AmbiguousAssetError, match="canonical identifier"):
        resolve_asset(AssetRequest(asset_type="crypto", symbol="SMALLTOKEN"))


def test_invalid_ethereum_contract_is_rejected():
    with pytest.raises(AssetValidationError, match="Ethereum contract"):
        resolve_asset(
            AssetRequest(
                asset_type="crypto",
                symbol="TOKEN",
                chain="ethereum",
                contract_address="0x1234",
            )
        )


def test_graph_resolves_and_serializes_legacy_crypto_identity():
    identity = TradingAgentsGraph.resolve_asset_identity("BTC-USD", "crypto")
    assert identity["canonical_id"] == "crypto:coingecko:bitcoin:spot:USD"
    assert identity["asset_type"] == "crypto"


def test_graph_state_retains_serializable_asset_identity():
    asset = resolve_asset(AssetRequest(symbol="BTC-USD"))
    state = Propagator().create_initial_state(
        "BTC-USD",
        "2026-08-14",
        asset_type="crypto",
        asset_identity=asset.to_state_dict(),
    )
    assert state["asset_identity"]["canonical_id"] == asset.canonical_id
    assert state["asset_identity"]["market_type"] == "spot"
    assert state["company_of_interest"] == "BTC-USD"
