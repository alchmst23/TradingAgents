from datetime import datetime, timezone

import requests

from tradingagents.assets import AssetRequest, resolve_asset
from tradingagents.dataflows.crypto_infra import HTTPPolicy
from tradingagents.dataflows.geckoterminal import GeckoTerminalAdapter


NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
TOKEN = "0x0c03ce270b4826ec62e7dd007f0b716068639f7b"
ASSET = resolve_asset(
    AssetRequest(symbol="CULT", asset_type="crypto", chain="base", contract_address=TOKEN)
)


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.content = b"{}"

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def payload():
    return {
        "data": [
            {
                "id": "base_0xpool1",
                "type": "pool",
                "attributes": {
                    "address": "0xpool1",
                    "name": "CULT / WETH",
                    "base_token_price_usd": "0.0000123",
                    "quote_token_price_usd": "4567.89",
                    "reserve_in_usd": "250000.5",
                    "fdv_usd": "1230000",
                    "market_cap_usd": "1000000",
                    "pool_created_at": "2025-01-02T03:04:05Z",
                    "volume_usd": {"h24": "65432.1"},
                    "price_change_percentage": {"h24": "4.2"},
                    "transactions": {"h24": {"buys": 111, "sells": 77}},
                },
                "relationships": {
                    "base_token": {"data": {"id": f"base_{TOKEN}"}},
                    "quote_token": {"data": {"id": "base_0x4200000000000000000000000000000000000006"}},
                    "dex": {"data": {"id": "aerodrome-base"}},
                },
            },
            {
                "id": "base_0xunrelated",
                "type": "pool",
                "attributes": {"reserve_in_usd": "999999999"},
                "relationships": {
                    "base_token": {"data": {"id": "base_0x1111111111111111111111111111111111111111"}},
                    "quote_token": {"data": {"id": "base_0x2222222222222222222222222222222222222222"}},
                },
            },
        ]
    }


def test_fetch_uses_chain_and_contract_and_normalizes_verified_pool():
    session = FakeSession([FakeResponse(payload())])
    observations, errors = GeckoTerminalAdapter(session=session, clock=lambda: NOW).fetch(ASSET)
    assert errors == ()
    assert len(observations) == 1
    dex = observations[0]
    assert dex.data_type == "dex"
    assert dex.payload["pool_address"] == "0xpool1"
    assert dex.payload["dex_id"] == "aerodrome-base"
    assert dex.payload["price_usd"] == 0.0000123
    assert dex.payload["liquidity_usd"] == 250000.5
    assert dex.payload["volume_usd_24h"] == 65432.1
    assert dex.payload["transactions_24h"] == {"buys": 111, "sells": 77}
    assert TOKEN in session.calls[0][0]
    assert "/networks/base/tokens/" in session.calls[0][0]


def test_unrelated_pool_is_never_selected():
    body = payload()
    body["data"] = [body["data"][1]]
    observations, errors = GeckoTerminalAdapter(
        session=FakeSession([FakeResponse(body)]), clock=lambda: NOW
    ).fetch(ASSET)
    assert observations == ()
    assert errors[0].code == "identity_mismatch"


def test_missing_contract_degrades_without_network_call():
    btc = resolve_asset(AssetRequest(symbol="BTC", asset_type="crypto"))
    session = FakeSession([])
    observations, errors = GeckoTerminalAdapter(session=session, clock=lambda: NOW).fetch(btc)
    assert observations == ()
    assert errors[0].code == "unsupported_identity"
    assert session.calls == []


def test_network_failure_is_sanitized():
    policy = HTTPPolicy(("api.geckoterminal.com",), max_retries=0)
    observations, errors = GeckoTerminalAdapter(
        session=FakeSession([requests.Timeout("api_key=secret")]),
        policy=policy,
        clock=lambda: NOW,
    ).fetch(ASSET)
    assert observations == ()
    assert errors[0].retryable is True
    assert "secret" not in errors[0].message


def test_success_is_cached():
    session = FakeSession([FakeResponse(payload())])
    adapter = GeckoTerminalAdapter(session=session, clock=lambda: NOW)
    assert adapter.fetch(ASSET) == adapter.fetch(ASSET)
    assert len(session.calls) == 1
