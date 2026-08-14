"""Hyperliquid vendor: symbol resolution, candle parsing, error mapping, and
router integration.

All API access is mocked, so these run without a network connection.
"""
import copy
import unittest
from unittest import mock

import pytest

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.dataflows import hyperliquid, interface
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorRateLimitError


def _candle(date_str, o, h, low, c, v):
    import pandas as pd

    t = int(pd.Timestamp(date_str, tz="UTC").timestamp() * 1000)
    return {"t": t, "o": str(o), "h": str(h), "l": str(low), "c": str(c), "v": str(v)}


_CANDLES = [
    _candle("2026-08-01", 62857, 63115, 62237, 62790, 10650.9),
    _candle("2026-08-02", 62789, 63784, 62786, 63556, 17904.7),
    _candle("2026-08-03", 63557, 64050, 62270, 63492, 27435.3),
]


@pytest.mark.unit
class HyperliquidSymbolResolutionTests(unittest.TestCase):
    def test_known_crypto_base_resolves(self):
        self.assertEqual(hyperliquid._hl_coin("BTC-USD"), "BTC")
        self.assertEqual(hyperliquid._hl_coin("ETHUSDT"), "ETH")

    def test_bare_coin_passthrough(self):
        self.assertEqual(hyperliquid._hl_coin("SOL"), "SOL")

    def test_generic_quote_suffix_stripped(self):
        self.assertEqual(hyperliquid._hl_coin("HYPE-USD"), "HYPE")
        self.assertEqual(hyperliquid._hl_coin("FARTCOIN-PERP"), "FARTCOIN")

    def test_scaled_meme_coin_keeps_lowercase_k_prefix(self):
        self.assertEqual(hyperliquid._hl_coin("kPEPE"), "kPEPE")
        self.assertEqual(hyperliquid._hl_coin("kpepe-usd"), "kPEPE")

    def test_legitimate_k_ticker_not_lowercased(self):
        # KAS (Kaspa) is a real, distinct ticker -- must not collide with the
        # scaled-coin "k" prefix rewrite.
        self.assertEqual(hyperliquid._hl_coin("KAS"), "KAS")


@pytest.mark.unit
class HyperliquidStockDataTests(unittest.TestCase):
    def test_candles_render_as_csv(self):
        with mock.patch.object(hyperliquid, "_fetch_candles") as fetch:
            import pandas as pd

            fetch.return_value = pd.DataFrame(
                [
                    {
                        "Date": pd.Timestamp(c["t"] // 1000, unit="s"),
                        "Open": float(c["o"]),
                        "High": float(c["h"]),
                        "Low": float(c["l"]),
                        "Close": float(c["c"]),
                        "Volume": float(c["v"]),
                    }
                    for c in _CANDLES
                ]
            )
            out = hyperliquid.get_stock_data("BTC-USD", "2026-08-01", "2026-08-03")
        self.assertIn("Hyperliquid OHLCV for BTC (from BTC-USD)", out)
        self.assertIn("62790.0", out)
        self.assertIn("Total records: 3", out)

    def test_empty_candles_raise_no_market_data(self):
        with (
            mock.patch.object(hyperliquid, "_fetch_candles", side_effect=NoMarketDataError("XXX", "XXX", "no candles")),
            self.assertRaises(NoMarketDataError),
        ):
            hyperliquid.get_stock_data("XXX", "2026-08-01", "2026-08-03")


@pytest.mark.unit
class HyperliquidCandleFetchTests(unittest.TestCase):
    def test_rate_limit_maps_to_vendor_rate_limit_error(self):
        response = mock.Mock(status_code=429)
        with (
            mock.patch("tradingagents.dataflows.hyperliquid.requests.post", return_value=response),
            self.assertRaises(VendorRateLimitError),
        ):
            hyperliquid._fetch_candles("BTC", "2026-08-01", "2026-08-03")

    def test_empty_response_raises_no_market_data(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = []
        response.raise_for_status.return_value = None
        with (
            mock.patch("tradingagents.dataflows.hyperliquid.requests.post", return_value=response),
            self.assertRaises(NoMarketDataError),
        ):
            hyperliquid._fetch_candles("ZZZ", "2026-08-01", "2026-08-03")


@pytest.mark.unit
class HyperliquidRoutingTests(unittest.TestCase):
    def setUp(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def tearDown(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def test_hyperliquid_no_data_falls_back_to_yfinance(self):
        set_config({"data_vendors": {"core_stock_apis": "hyperliquid,yfinance"}})
        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {
                "get_stock_data": {
                    "hyperliquid": mock.Mock(side_effect=NoMarketDataError("AAPL", "AAPL", "unknown coin")),
                    "yfinance": lambda *a, **k: "YF_OK",
                }
            },
            clear=False,
        ):
            out = interface.route_to_vendor("get_stock_data", "AAPL", "2026-08-01", "2026-08-03")
        self.assertEqual(out, "YF_OK")

    def test_hyperliquid_success_short_circuits_yfinance(self):
        set_config({"data_vendors": {"core_stock_apis": "hyperliquid,yfinance"}})
        yf_impl = mock.Mock(return_value="YF_OK")
        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {
                "get_stock_data": {
                    "hyperliquid": lambda *a, **k: "HL_OK",
                    "yfinance": yf_impl,
                }
            },
            clear=False,
        ):
            out = interface.route_to_vendor("get_stock_data", "BTC-USD", "2026-08-01", "2026-08-03")
        self.assertEqual(out, "HL_OK")
        yf_impl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
