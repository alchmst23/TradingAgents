import json
import subprocess
from datetime import datetime, timedelta, timezone

from tradingagents.assets import AssetRequest, resolve_asset
from tradingagents.dataflows.donna_x_sentiment import (
    BuzzCliTransport,
    DONNA_PUBKEY,
    DonnaXSentimentAdapter,
)

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
BTC = resolve_asset(AssetRequest(symbol="BTC-USD", asset_type="crypto"))


class StubTransport:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []

    def exchange(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.response


def valid_response(**overrides):
    payload = {
        "type": "x_sentiment_response",
        "schema_version": "1.0",
        "request_id": "req-1",
        "canonical_asset_id": BTC.canonical_id,
        "generated_at": NOW.isoformat(),
        "posts": [
            {
                "post_id": "1890",
                "created_at": (NOW - timedelta(minutes=2)).isoformat(),
                "text": "BTC momentum remains strong",
                "author": "analyst",
                "metrics": {"likes": 8, "reposts": 2, "replies": 1},
            }
        ],
        "aggregate": {"score": 0.6, "label": "bullish", "post_count": 1},
        "provenance": {"source": "x_recent_search", "custodian": "Donna"},
    }
    payload.update(overrides)
    return {"pubkey": DONNA_PUBKEY, "content": json.dumps(payload)}


def test_builds_bounded_identity_aware_request_and_normalizes_response(monkeypatch):
    transport = StubTransport(valid_response())
    monkeypatch.setattr("tradingagents.dataflows.donna_x_sentiment.uuid4", lambda: type("U", (), {"hex": "req-1"})())
    adapter = DonnaXSentimentAdapter(transport=transport, clock=lambda: NOW, lookback=timedelta(hours=24), max_posts=20)

    observations, errors = adapter.fetch(BTC)

    assert errors == ()
    assert len(observations) == 1
    item = observations[0]
    assert item.provider == "donna_x"
    assert item.data_type == "sentiment"
    assert item.payload["aggregate"]["score"] == 0.6
    assert item.provenance["responder_pubkey"] == DONNA_PUBKEY
    request = transport.requests[0]
    assert request["request_id"] == "req-1"
    assert request["canonical_asset_id"] == BTC.canonical_id
    assert request["symbol"] == "BTC"
    assert request["max_posts"] == 20
    assert request["start_time"] == (NOW - timedelta(hours=24)).isoformat()
    assert "credentials" not in json.dumps(request).lower()


def test_rejects_response_from_wrong_identity():
    response = valid_response()
    response["pubkey"] = "0" * 64
    observations, errors = DonnaXSentimentAdapter(transport=StubTransport(response), clock=lambda: NOW).fetch(BTC)
    assert observations == ()
    assert errors[0].code == "untrusted_responder"
    assert errors[0].retryable is False


def test_rejects_mismatched_request_or_asset_identity():
    for override in ({"request_id": "other"}, {"canonical_asset_id": "crypto:wrong"}):
        transport = StubTransport(valid_response(**override))
        adapter = DonnaXSentimentAdapter(transport=transport, clock=lambda: NOW, request_id_factory=lambda: "req-1")
        observations, errors = adapter.fetch(BTC)
        assert observations == ()
        assert errors[0].code == "invalid_response"


def test_timeout_degrades_to_sanitized_retryable_error():
    adapter = DonnaXSentimentAdapter(
        transport=StubTransport(error=TimeoutError("token=secret-value")),
        clock=lambda: NOW,
    )
    observations, errors = adapter.fetch(BTC)
    assert observations == ()
    assert errors[0].code == "donna_timeout"
    assert errors[0].retryable is True
    assert "secret-value" not in errors[0].message


def test_malformed_or_oversized_response_is_rejected():
    malformed = {"pubkey": DONNA_PUBKEY, "content": "not-json"}
    oversized = {"pubkey": DONNA_PUBKEY, "content": "x" * 70_000}
    for response in (malformed, oversized):
        observations, errors = DonnaXSentimentAdapter(transport=StubTransport(response), clock=lambda: NOW).fetch(BTC)
        assert observations == ()
        assert errors[0].code == "invalid_response"


def test_post_count_and_score_are_strictly_bounded():
    bad_posts = valid_response(posts=[valid_response() for _ in range(101)])
    bad_score = valid_response(aggregate={"score": 1.1, "label": "bullish", "post_count": 1})
    for response in (bad_posts, bad_score):
        observations, errors = DonnaXSentimentAdapter(transport=StubTransport(response), clock=lambda: NOW).fetch(BTC)
        assert observations == ()
        assert errors[0].code == "invalid_response"


def test_buzz_transport_accepts_only_donnas_correlated_reply():
    calls = []
    outputs = iter(
        (
            {"event_id": "event-1"},
            [
                {"pubkey": "attacker", "tags": [["e", "event-1"]], "content": "{}"},
                {"pubkey": DONNA_PUBKEY, "tags": [["e", "other"]], "content": "{}"},
                {"pubkey": DONNA_PUBKEY, "tags": [["e", "event-1", "", "reply"]], "content": "{\"ok\":true}"},
            ],
        )
    )

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(next(outputs)), stderr="")

    ticks = iter((0.0, 0.1))
    transport = BuzzCliTransport(runner=runner, monotonic=lambda: next(ticks), sleeper=lambda _: None)

    reply = transport.exchange({"request_id": "req-1"})

    assert reply["pubkey"] == DONNA_PUBKEY
    assert calls[0][0][1:3] == ["messages", "send"]
    assert DONNA_PUBKEY in calls[0][0]
    assert calls[1][0][1:3] == ["messages", "get"]
    assert all(call[1]["check"] is True for call in calls)
