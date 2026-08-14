"""Official X recent-search fetcher tests; all HTTP access is mocked."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest
import requests

from tradingagents.dataflows import x_posts


def _response(payload, status_code=200):
    response = Mock(status_code=status_code)
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


@pytest.mark.unit
class TestXSearchQuery:
    @pytest.mark.parametrize(
        ("ticker", "expected"),
        [
            ("NVDA", "($NVDA OR #NVDA) lang:en -is:retweet"),
            ("$AAPL", "($AAPL OR #AAPL) lang:en -is:retweet"),
            ("BTC-USD", "($BTC OR #BTC) lang:en -is:retweet"),
            ("eth-usdt", "($ETH OR #ETH) lang:en -is:retweet"),
        ],
    )
    def test_query_is_ticker_aware(self, ticker, expected):
        assert x_posts._build_query(ticker) == expected


@pytest.mark.unit
class TestXCredentialsAndErrors:
    def test_missing_token_skips_network(self, monkeypatch):
        monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
        with patch.object(x_posts.requests, "get") as get:
            out = x_posts.fetch_x_posts("NVDA")
        get.assert_not_called()
        assert "X_BEARER_TOKEN is not set" in out

    @pytest.mark.parametrize(
        ("status", "message"),
        [
            (401, "authentication failed"),
            (402, "credits are unavailable"),
            (403, "not permitted"),
            (429, "rate limit"),
        ],
    )
    def test_known_http_errors_are_actionable(self, status, message):
        with patch.object(x_posts.requests, "get", return_value=_response({}, status)):
            out = x_posts.fetch_x_posts("NVDA", bearer_token="secret")
        assert message in out
        assert "secret" not in out

    def test_transport_error_degrades_to_placeholder(self):
        with patch.object(
            x_posts.requests, "get", side_effect=requests.Timeout("slow")
        ):
            out = x_posts.fetch_x_posts("NVDA", bearer_token="secret")
        assert out == "<X unavailable: Timeout>"


@pytest.mark.unit
class TestXRecentWindow:
    def test_bounds_dates_to_rolling_seven_days(self):
        now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        params = x_posts._recent_time_params("2026-08-07", "2026-08-14", now=now)
        assert params == {
            "start_time": "2026-08-07T12:01:00Z",
            "end_time": "2026-08-14T12:00:00Z",
        }

    def test_historical_range_is_rejected_before_billable_request(self):
        with patch.object(x_posts.requests, "get") as get:
            out = x_posts.fetch_x_posts(
                "NVDA",
                "2024-05-03",
                "2024-05-10",
                bearer_token="secret",
            )
        get.assert_not_called()
        assert "outside X's rolling seven-day window" in out


@pytest.mark.unit
class TestXFormatting:
    def test_fetches_official_fields_and_formats_engagement(self):
        payload = {
            "data": [
                {
                    "id": "1",
                    "text": "NVDA momentum remains strong",
                    "created_at": "2026-08-14T12:00:00.000Z",
                    "author_id": "42",
                    "public_metrics": {
                        "like_count": 12,
                        "retweet_count": 3,
                        "reply_count": 2,
                        "quote_count": 1,
                    },
                }
            ],
            "includes": {
                "users": [{"id": "42", "username": "marketwatcher", "verified": True}]
            },
        }
        with patch.object(x_posts.requests, "get", return_value=_response(payload)) as get:
            out = x_posts.fetch_x_posts("NVDA", limit=30, bearer_token="secret")

        _, kwargs = get.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer secret"
        assert kwargs["params"]["max_results"] == 30
        assert kwargs["params"]["expansions"] == "author_id"
        assert "public_metrics" in kwargs["params"]["tweet.fields"]
        assert "@marketwatcher · verified" in out
        assert "12 likes · 3 reposts · 2 replies · 1 quotes" in out
        assert "NVDA momentum remains strong" in out

    def test_empty_results_are_explicit(self):
        with patch.object(x_posts.requests, "get", return_value=_response({"meta": {}})):
            out = x_posts.fetch_x_posts("BTC-USD", bearer_token="secret")
        assert out == "<no recent X posts found for $BTC>"
