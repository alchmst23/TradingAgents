"""Identity-verified Donna X sentiment evidence boundary.

Donna retains custody of X credentials. This adapter exchanges only bounded,
analysis-only request and response documents through an injected Buzz transport.
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

from tradingagents.assets import ResolvedAsset
from tradingagents.evidence import Observation, ProviderError

DONNA_PUBKEY = "f8674a99af251588835dd750de18f6cd812c9e4c32007a46b82f2156af2c138c"
DONNA_CHANNEL_ID = "0fca20b0-1574-4e09-b978-f44051d5ae0c"
MAX_RESPONSE_BYTES = 65_536
MAX_POSTS = 100


class DonnaTransport(Protocol):
    def exchange(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


class BuzzCliTransport:
    """Bounded request/reply transport using Brian's own Buzz environment."""

    def __init__(
        self,
        *,
        executable: str = "buzz",
        channel_id: str = DONNA_CHANNEL_ID,
        donna_pubkey: str = DONNA_PUBKEY,
        timeout_seconds: float = 30.0,
        poll_seconds: float = 1.0,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 1 and 120")
        self.executable = executable
        self.channel_id = channel_id
        self.donna_pubkey = donna_pubkey
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        self.runner = runner
        self.monotonic = monotonic
        self.sleeper = sleeper

    def exchange(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        message = "@Donna " + json.dumps(request, separators=(",", ":"), sort_keys=True)
        sent = self._run("messages", "send", "--channel", self.channel_id, "--mention", self.donna_pubkey, "--content", message)
        event_id = json.loads(sent.stdout).get("event_id")
        if not isinstance(event_id, str):
            raise RuntimeError("Buzz send did not return an event id")
        deadline = self.monotonic() + self.timeout_seconds
        while self.monotonic() < deadline:
            received = self._run("messages", "get", "--channel", self.channel_id, "--limit", "25")
            messages = json.loads(received.stdout)
            for item in reversed(messages):
                if item.get("pubkey") != self.donna_pubkey:
                    continue
                tags = item.get("tags", [])
                if any(len(tag) >= 2 and tag[0] == "e" and tag[1] == event_id for tag in tags):
                    return item
            self.sleeper(self.poll_seconds)
        raise TimeoutError("bounded Buzz reply wait expired")

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.runner(
            [self.executable, *args], capture_output=True, text=True,
            timeout=min(self.timeout_seconds, 15), check=True,
        )


def _utc(value: str, name: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{name} must be UTC")
    return parsed


class DonnaXSentimentAdapter:
    def __init__(
        self,
        *,
        transport: DonnaTransport,
        clock: Callable[[], datetime] | None = None,
        lookback: timedelta = timedelta(hours=24),
        max_posts: int = 50,
        request_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not timedelta(minutes=1) <= lookback <= timedelta(days=7):
            raise ValueError("lookback must be between one minute and seven days")
        if not 1 <= max_posts <= MAX_POSTS:
            raise ValueError(f"max_posts must be between 1 and {MAX_POSTS}")
        self.transport = transport
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lookback = lookback
        self.max_posts = max_posts
        self.request_id_factory = request_id_factory or (lambda: uuid4().hex)

    def fetch(self, asset: ResolvedAsset) -> tuple[tuple[Observation, ...], tuple[ProviderError, ...]]:
        now = self.clock()
        request_id = self.request_id_factory()
        request = {
            "type": "x_sentiment_request",
            "schema_version": "1.0",
            "request_id": request_id,
            "canonical_asset_id": asset.canonical_id,
            "symbol": self._query_symbol(asset),
            "contract_address": asset.contract_address,
            "chain": asset.chain,
            "start_time": (now - self.lookback).isoformat(),
            "end_time": now.isoformat(),
            "max_posts": self.max_posts,
        }
        try:
            envelope = self.transport.exchange(request)
            observation = self._parse(envelope, asset, request_id, now)
            return (observation,), ()
        except TimeoutError as exc:
            return (), (self._error("donna_timeout", f"Donna did not reply: {exc}", now, True),)
        except _UntrustedResponder as exc:
            return (), (self._error("untrusted_responder", str(exc), now, False),)
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            return (), (self._error("invalid_response", f"Donna response rejected: {exc}", now, False),)
        except Exception as exc:
            return (), (self._error("donna_unavailable", f"Donna transport unavailable: {exc}", now, True),)

    def _parse(self, envelope: Mapping[str, Any], asset: ResolvedAsset, request_id: str, now: datetime) -> Observation:
        if envelope.get("pubkey") != DONNA_PUBKEY:
            raise _UntrustedResponder("response signer does not match Donna")
        content = envelope.get("content")
        if not isinstance(content, str) or len(content.encode()) > MAX_RESPONSE_BYTES:
            raise ValueError("response content is absent or oversized")
        body = json.loads(content)
        if body.get("type") != "x_sentiment_response" or body.get("schema_version") != "1.0":
            raise ValueError("unsupported response type or schema")
        if body.get("request_id") != request_id or body.get("canonical_asset_id") != asset.canonical_id:
            raise ValueError("response correlation or asset identity mismatch")
        generated_at = _utc(body["generated_at"], "generated_at")
        posts = body.get("posts")
        aggregate = body.get("aggregate")
        provenance = body.get("provenance")
        if not isinstance(posts, list) or len(posts) > MAX_POSTS:
            raise ValueError("posts must be a bounded list")
        if not isinstance(aggregate, dict) or not isinstance(provenance, dict):
            raise ValueError("aggregate and provenance objects are required")
        score = aggregate.get("score")
        count = aggregate.get("post_count")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not -1 <= score <= 1:
            raise ValueError("aggregate score must be between -1 and 1")
        if not isinstance(count, int) or count != len(posts):
            raise ValueError("aggregate post_count must match posts")
        for post in posts:
            if not isinstance(post, dict) or not isinstance(post.get("post_id"), str):
                raise ValueError("each post requires a string post_id")
            _utc(post["created_at"], "post.created_at")
        return Observation(
            provider="donna_x",
            canonical_asset_id=asset.canonical_id,
            data_type="sentiment",
            observed_at=now,
            source_timestamp=generated_at,
            quote_currency=asset.quote_currency,
            market_type=asset.market_type,
            payload={"posts": posts, "aggregate": aggregate},
            provenance={**provenance, "responder_pubkey": DONNA_PUBKEY, "channel_id": DONNA_CHANNEL_ID},
        )

    @staticmethod
    def _query_symbol(asset: ResolvedAsset) -> str:
        suffix = f"-{asset.quote_currency}"
        if asset.display_symbol.upper().endswith(suffix.upper()):
            return asset.display_symbol[: -len(suffix)]
        return asset.display_symbol

    @staticmethod
    def _error(code: str, message: str, now: datetime, retryable: bool) -> ProviderError:
        return ProviderError.sanitized(
            provider="donna_x", code=code, message=message, observed_at=now,
            retryable=retryable, data_type="sentiment",
        )


class _UntrustedResponder(ValueError):
    pass
