from datetime import datetime, timezone

import requests

from tradingagents.assets import AssetRequest, resolve_asset
from tradingagents.dataflows.crypto_infra import HTTPPolicy
from tradingagents.dataflows.hyperliquid_derivatives import HyperliquidDerivativesAdapter


NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
ASSET = resolve_asset(
    AssetRequest(
        symbol="BTC",
        asset_type="crypto",
        venue="hyperliquid",
        instrument="BTC",
        market_type="perpetual",
        quote_currency="USDC",
    )
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

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def responses():
    return [
        FakeResponse(
            [
                {
                    "universe": [
                        {
                            "name": "BTC",
                            "szDecimals": 5,
                            "maxLeverage": 40,
                            "onlyIsolated": False,
                        },
                        {"name": "ETH", "szDecimals": 4, "maxLeverage": 25},
                    ]
                },
                [
                    {
                        "dayNtlVlm": "123456789.12",
                        "funding": "0.0000125",
                        "impactPxs": ["60100.0", "60102.0"],
                        "markPx": "60101.0",
                        "midPx": "60100.5",
                        "openInterest": "12345.67",
                        "oraclePx": "60095.0",
                        "premium": "0.00009984",
                        "prevDayPx": "59000.0",
                    },
                    {"markPx": "3100.0"},
                ],
            ]
        ),
        FakeResponse(
            [
                {"coin": "BTC", "fundingRate": "0.00001", "premium": "0.00008", "time": 1786791600000},
                {"coin": "BTC", "fundingRate": "0.00002", "premium": "0.00009", "time": 1786795200000},
            ]
        ),
        FakeResponse(
            {
                "coin": "BTC",
                "time": 1786795200000,
                "levels": [
                    [
                        {"px": "60100.0", "sz": "2.5", "n": 4},
                        {"px": "60099.0", "sz": "3.0", "n": 2},
                    ],
                    [
                        {"px": "60102.0", "sz": "1.5", "n": 3},
                        {"px": "60103.0", "sz": "4.0", "n": 2},
                    ],
                ],
            }
        ),
    ]


def test_fetch_normalizes_market_funding_and_bounded_book_evidence():
    session = FakeSession(responses())
    observations, errors = HyperliquidDerivativesAdapter(
        session=session,
        clock=lambda: NOW,
        max_book_levels=1,
    ).fetch(ASSET)

    assert errors == ()
    assert [item.data_type for item in observations] == [
        "derivatives_market",
        "funding_history",
        "order_book",
    ]
    market, funding, book = observations
    assert market.canonical_asset_id == "crypto:hyperliquid:BTC:perpetual:USDC"
    assert market.market_type.value == "perpetual"
    assert market.payload["mark_price"] == 60101.0
    assert market.payload["oracle_price"] == 60095.0
    assert market.payload["open_interest_base"] == 12345.67
    assert market.payload["open_interest_usd"] == 741987112.67
    assert market.payload["funding_rate"] == 0.0000125
    assert market.payload["basis_to_oracle_percentage"] == 0.009984191696488232
    assert market.payload["size_decimals"] == 5
    assert market.payload["max_leverage"] == 40
    assert len(funding.payload["records"]) == 2
    assert funding.source_timestamp == datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    assert book.payload["spread"] == 2.0
    assert book.payload["spread_bps"] == 0.33277316517196054
    assert book.payload["bids"] == ({"price": 60100.0, "size": 2.5, "orders": 4},)
    assert book.payload["asks"] == ({"price": 60102.0, "size": 1.5, "orders": 3},)
    assert all(call[0] == "https://api.hyperliquid.xyz/info" for call in session.calls)
    assert [call[1]["json"]["type"] for call in session.calls] == [
        "metaAndAssetCtxs",
        "fundingHistory",
        "l2Book",
    ]


def test_non_hyperliquid_perpetual_is_rejected_without_network():
    spot = resolve_asset(AssetRequest(symbol="BTC", asset_type="crypto"))
    session = FakeSession([])
    observations, errors = HyperliquidDerivativesAdapter(
        session=session, clock=lambda: NOW
    ).fetch(spot)
    assert observations == ()
    assert errors[0].code == "unsupported_identity"
    assert session.calls == []


def test_unknown_instrument_returns_typed_error():
    unknown = resolve_asset(
        AssetRequest(
            symbol="ZZZ",
            asset_type="crypto",
            venue="hyperliquid",
            instrument="ZZZ",
            market_type="perpetual",
        )
    )
    session = FakeSession([FakeResponse(responses()[0].payload)])
    observations, errors = HyperliquidDerivativesAdapter(
        session=session, clock=lambda: NOW
    ).fetch(unknown)
    assert observations == ()
    assert errors[0].code == "instrument_not_found"


def test_provider_timeout_is_sanitized_and_degrades():
    policy = HTTPPolicy(("api.hyperliquid.xyz",), max_retries=0)
    observations, errors = HyperliquidDerivativesAdapter(
        session=FakeSession([requests.Timeout("authorization=secret")]),
        policy=policy,
        clock=lambda: NOW,
    ).fetch(ASSET)
    assert observations == ()
    assert errors[0].retryable is True
    assert "secret" not in errors[0].message
