from datetime import datetime, timezone

import requests

from tradingagents.assets import AssetRequest, resolve_asset
from tradingagents.dataflows.coingecko import CoinGeckoAdapter
from tradingagents.dataflows.crypto_infra import HTTPPolicy


NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
BTC = resolve_asset(AssetRequest(asset_type="crypto", coingecko_id="bitcoin", symbol="BTC"))
BASE_ETH = resolve_asset(
    AssetRequest(
        asset_type="crypto",
        symbol="ETH",
        chain="base",
        contract_address="0x4200000000000000000000000000000000000006",
    )
)


class FakeResponse:
    def __init__(self, payload, status_code=200, content=None):
        self._payload = payload
        self.status_code = status_code
        self.content = content if content is not None else b"{}"
        self.text = "response body must not leak"

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def payload():
    return {
        "id": "bitcoin",
        "symbol": "btc",
        "name": "Bitcoin",
        "asset_platform_id": None,
        "contract_address": "",
        "categories": ["Layer 1"],
        "description": {"en": "Bitcoin description"},
        "links": {"homepage": ["https://bitcoin.org"]},
        "market_data": {
            "current_price": {"usd": 65000.0},
            "market_cap": {"usd": 1_280_000_000_000},
            "total_volume": {"usd": 20_000_000_000},
            "price_change_percentage_24h": 2.5,
            "circulating_supply": 19_700_000,
            "total_supply": 21_000_000,
        },
        "last_updated": "2026-08-15T11:59:30.000Z",
    }


def test_fetch_normalizes_metadata_and_market_observations():
    session = FakeSession([FakeResponse(payload())])
    observations, errors = CoinGeckoAdapter(session=session, clock=lambda: NOW).fetch(BTC)
    assert errors == ()
    assert [item.data_type for item in observations] == ["metadata", "market"]
    metadata, market = observations
    assert metadata.canonical_asset_id == BTC.canonical_id
    assert metadata.payload["coingecko_id"] == "bitcoin"
    assert metadata.provenance["endpoint"] == "/api/v3/coins/{id}"
    assert market.payload["price"] == 65000.0
    assert market.payload["market_cap"] == 1_280_000_000_000
    assert market.quote_currency == "USD"
    assert market.source_timestamp.isoformat() == "2026-08-15T11:59:30+00:00"
    assert session.calls[0][1]["timeout"] == (5.0, 20.0)


def test_contract_identity_uses_chain_scoped_contract_endpoint():
    session = FakeSession([FakeResponse(payload())])
    CoinGeckoAdapter(session=session, clock=lambda: NOW).fetch(BASE_ETH)
    assert session.calls[0][0].endswith(
        "/api/v3/coins/base/contract/0x4200000000000000000000000000000000000006"
    )


def test_cache_avoids_duplicate_provider_calls():
    session = FakeSession([FakeResponse(payload())])
    adapter = CoinGeckoAdapter(session=session, clock=lambda: NOW)
    assert adapter.fetch(BTC) == adapter.fetch(BTC)
    assert len(session.calls) == 1


def test_network_failure_degrades_to_sanitized_provider_error():
    session = FakeSession([requests.Timeout("token=super-secret")])
    observations, errors = CoinGeckoAdapter(
        session=session,
        clock=lambda: NOW,
        policy=HTTPPolicy(("api.coingecko.com",), max_retries=0),
    ).fetch(BTC)
    assert observations == ()
    assert len(errors) == 1
    assert errors[0].provider == "coingecko"
    assert errors[0].retryable is True
    assert "super-secret" not in errors[0].message
    assert "[REDACTED]" in errors[0].message


def test_malformed_payload_degrades_without_fabricating_market_values():
    session = FakeSession([FakeResponse({"id": "bitcoin", "market_data": {}})])
    observations, errors = CoinGeckoAdapter(session=session, clock=lambda: NOW).fetch(BTC)
    assert observations == ()
    assert errors[0].code == "invalid_payload"
