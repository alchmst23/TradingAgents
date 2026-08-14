"""Hyperliquid vendor: OHLCV price data and derived technical indicators for
perpetual/spot symbols traded on Hyperliquid.

Uses Hyperliquid's public Info API (https://api.hyperliquid.xyz/info,
``candleSnapshot`` request type) — no key, no auth. Hyperliquid has no
indicator endpoint of its own, so indicators are computed locally with
stockstats from the fetched candles, the same approach the yfinance vendor
uses (see ``y_finance.get_stock_stats_indicators_window``).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

import pandas as pd
import requests
from stockstats import wrap

from .errors import NoMarketDataError, VendorRateLimitError
from .symbol_utils import crypto_base

API_URL = "https://api.hyperliquid.xyz/info"

# Network timeout (seconds), consistent with the other vendors.
REQUEST_TIMEOUT = 30

# Extra calendar days fetched before the requested lookback window so
# early-window indicators (e.g. a 200 SMA) have enough warm-up candles.
INDICATOR_WARMUP_DAYS = 210

# Hyperliquid lists these "1000x scaled" meme-coin perps with a lowercase
# "k" prefix (kPEPE, kBONK, ...) — not to be confused with legitimately
# K-prefixed tickers like KAS or KSM. The API is case-sensitive, so map the
# user's case-insensitive input back to Hyperliquid's exact listed symbol.
_SCALED_PREFIX_COINS = {
    "KPEPE": "kPEPE",
    "KBONK": "kBONK",
    "KSHIB": "kSHIB",
    "KFLOKI": "kFLOKI",
    "KLUNC": "kLUNC",
    "KNEIRO": "kNEIRO",
    "KDOGS": "kDOGS",
}

_INDICATOR_DESCRIPTIONS = {
    "close_50_sma": (
        "50 SMA: A medium-term trend indicator. "
        "Usage: Identify trend direction and serve as dynamic support/resistance. "
        "Tips: It lags price; combine with faster indicators for timely signals."
    ),
    "close_200_sma": (
        "200 SMA: A long-term trend benchmark. "
        "Usage: Confirm overall market trend and identify golden/death cross setups. "
        "Tips: It reacts slowly; best for strategic trend confirmation rather than frequent trading entries."
    ),
    "close_10_ema": (
        "10 EMA: A responsive short-term average. "
        "Usage: Capture quick shifts in momentum and potential entry points. "
        "Tips: Prone to noise in choppy markets; use alongside longer averages for filtering false signals."
    ),
    "macd": (
        "MACD: Computes momentum via differences of EMAs. "
        "Usage: Look for crossovers and divergence as signals of trend changes. "
        "Tips: Confirm with other indicators in low-volatility or sideways markets."
    ),
    "macds": (
        "MACD Signal: An EMA smoothing of the MACD line. "
        "Usage: Use crossovers with the MACD line to trigger trades. "
        "Tips: Should be part of a broader strategy to avoid false positives."
    ),
    "macdh": (
        "MACD Histogram: Shows the gap between the MACD line and its signal. "
        "Usage: Visualize momentum strength and spot divergence early. "
        "Tips: Can be volatile; complement with additional filters in fast-moving markets."
    ),
    "rsi": (
        "RSI: Measures momentum to flag overbought/oversold conditions. "
        "Usage: Apply 70/30 thresholds and watch for divergence to signal reversals. "
        "Tips: In strong trends, RSI may remain extreme; always cross-check with trend analysis."
    ),
    "boll": (
        "Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands. "
        "Usage: Acts as a dynamic benchmark for price movement. "
        "Tips: Combine with the upper and lower bands to effectively spot breakouts or reversals."
    ),
    "boll_ub": (
        "Bollinger Upper Band: Typically 2 standard deviations above the middle line. "
        "Usage: Signals potential overbought conditions and breakout zones. "
        "Tips: Confirm signals with other tools; prices may ride the band in strong trends."
    ),
    "boll_lb": (
        "Bollinger Lower Band: Typically 2 standard deviations below the middle line. "
        "Usage: Indicates potential oversold conditions. "
        "Tips: Use additional analysis to avoid false reversal signals."
    ),
    "atr": (
        "ATR: Averages true range to measure volatility. "
        "Usage: Set stop-loss levels and adjust position sizes based on current market volatility. "
        "Tips: It's a reactive measure, so use it as part of a broader risk management strategy."
    ),
    "vwma": (
        "VWMA: A moving average weighted by volume. "
        "Usage: Confirm trends by integrating price action with volume data. "
        "Tips: Watch for skewed results from volume spikes; use in combination with other volume analyses."
    ),
    "mfi": (
        "MFI: The Money Flow Index is a momentum indicator that uses both price and volume to measure buying and selling pressure. "
        "Usage: Identify overbought (>80) or oversold (<20) conditions and confirm the strength of trends or reversals. "
        "Tips: Use alongside RSI or MACD to confirm signals; divergence between price and MFI can indicate potential reversals."
    ),
}


def _hl_coin(symbol: str) -> str:
    """Resolve a user-typed symbol to a Hyperliquid coin name.

    Hyperliquid identifies assets by bare coin name (``BTC``, ``ETH``,
    ``kPEPE``, ...), not a quoted pair. Strip common quote suffixes
    (``-USD``, ``USDT``, ...) so symbols typed the way other vendors expect
    (``BTC-USD``, ``BTCUSD``) still resolve; anything else is passed through
    uppercased unchanged, since Hyperliquid lists hundreds of coins beyond
    the small cross-vendor crypto set in ``symbol_utils``.
    """
    s = symbol.strip().upper().rstrip("+")
    base = crypto_base(s)
    if base:
        return base
    compact = s.replace("-", "").replace("/", "")
    for suffix in ("PERP", "USDT", "USDC", "USD"):
        if compact.endswith(suffix) and len(compact) > len(suffix):
            compact = compact[: -len(suffix)]
            break
    return _SCALED_PREFIX_COINS.get(compact, compact)


def _to_ms(date_str: str, *, end_of_day: bool = False) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end_of_day:
        dt += timedelta(days=1)
    return int(dt.timestamp() * 1000)


def _fetch_candles(
    coin: str, start_date: str, end_date: str, interval: str = "1d"
) -> pd.DataFrame:
    """Fetch daily OHLCV candles for ``coin`` between the given dates.

    Raises:
        VendorRateLimitError: Hyperliquid throttled the request (HTTP 429).
        NoMarketDataError: the coin is unknown or has no candles in range.
    """
    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": interval,
            "startTime": _to_ms(start_date),
            # endTime is exclusive of any bar not yet opened by then; request
            # one day past end_date so the requested end_date bar is included.
            "endTime": _to_ms(end_date, end_of_day=True),
        },
    }
    response = requests.post(API_URL, json=payload, timeout=REQUEST_TIMEOUT)
    if response.status_code == 429:
        raise VendorRateLimitError(f"Hyperliquid rate limit exceeded for {coin!r}")
    response.raise_for_status()
    candles = response.json()

    if not candles:
        raise NoMarketDataError(
            coin, coin, f"no candles between {start_date} and {end_date}"
        )

    rows = [
        {
            "Date": pd.to_datetime(int(c["t"]), unit="ms", utc=True).tz_localize(None),
            "Open": float(c["o"]),
            "High": float(c["h"]),
            "Low": float(c["l"]),
            "Close": float(c["c"]),
            "Volume": float(c["v"]),
        }
        for c in candles
    ]
    return pd.DataFrame(rows).sort_values("Date").reset_index(drop=True)


def get_stock_data(
    symbol: Annotated[str, "coin symbol, e.g. BTC, ETH, or BTC-USD"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    datetime.strptime(start_date, "%Y-%m-%d")
    datetime.strptime(end_date, "%Y-%m-%d")

    coin = _hl_coin(symbol)
    data = _fetch_candles(coin, start_date, end_date)

    start_dt = pd.to_datetime(start_date)
    end_dt = pd.to_datetime(end_date)
    data = data[(data["Date"] >= start_dt) & (data["Date"] <= end_dt)]
    if data.empty:
        raise NoMarketDataError(
            symbol, coin, f"no rows between {start_date} and {end_date}"
        )

    csv_string = data.to_csv(index=False)

    label = coin if coin == symbol.strip().upper() else f"{coin} (from {symbol})"
    header = f"# Hyperliquid OHLCV for {label} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(data)}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

    return header + csv_string


def get_indicator(
    symbol: Annotated[str, "coin symbol, e.g. BTC, ETH, or BTC-USD"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    curr_date: Annotated[str, "The current trading date you are trading on, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:
    if indicator not in _INDICATOR_DESCRIPTIONS:
        raise ValueError(
            f"Indicator {indicator} is not supported. "
            f"Please choose from: {list(_INDICATOR_DESCRIPTIONS.keys())}"
        )

    curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    before = curr_date_dt - timedelta(days=look_back_days)
    fetch_start = (before - timedelta(days=INDICATOR_WARMUP_DAYS)).strftime("%Y-%m-%d")

    coin = _hl_coin(symbol)
    data = _fetch_candles(coin, fetch_start, curr_date)

    df = wrap(data.copy())
    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
    df[indicator]  # trigger stockstats calculation for the whole frame

    values = {}
    for _, row in df.iterrows():
        val = row[indicator]
        values[row["Date"]] = "N/A" if pd.isna(val) else str(val)

    lines = []
    d = curr_date_dt
    while d >= before:
        date_str = d.strftime("%Y-%m-%d")
        lines.append(f"{date_str}: {values.get(date_str, 'N/A: no candle for this date')}")
        d -= timedelta(days=1)

    return (
        f"## {indicator} values from {before.strftime('%Y-%m-%d')} to {curr_date}:\n\n"
        + "\n".join(lines)
        + "\n\n"
        + _INDICATOR_DESCRIPTIONS[indicator]
    )
