"""Official X API recent-search fetcher for ticker-specific public posts.

Uses X API v2's ``/2/tweets/search/recent`` endpoint with OAuth 2.0 app-only
authentication. Set ``X_BEARER_TOKEN`` to enable it. The endpoint searches the
last seven days and may consume paid X API credits, so this fetcher makes one
bounded request per sentiment-analysis run and does not paginate.

The public function returns a prompt-ready plaintext block and degrades to an
explicit placeholder for missing credentials, API errors, or empty results.
It never exposes the bearer token in output or logs.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from .symbol_utils import crypto_base

logger = logging.getLogger(__name__)

_API = "https://api.x.com/2/tweets/search/recent"
_MIN_RESULTS = 10
_MAX_RESULTS = 100


def _search_symbol(ticker: str) -> str:
    """Return a cashtag-safe symbol, reducing crypto pairs to their base."""
    return (crypto_base(ticker) or ticker).strip().upper().lstrip("$")


def _build_query(ticker: str) -> str:
    """Build a focused English-language query without repost amplification."""
    symbol = _search_symbol(ticker)
    return f"(${symbol} OR #{symbol}) lang:en -is:retweet"


def _unavailable(reason: str) -> str:
    return f"<X unavailable: {reason}>"


def _metric(metrics: dict[str, Any], key: str) -> int:
    value = metrics.get(key, 0)
    return value if isinstance(value, int) else 0


def _recent_time_params(
    start_date: str | None,
    end_date: str | None,
    *,
    now: datetime | None = None,
) -> dict[str, str] | None:
    """Bound a requested date range to X's rolling seven-day search window.

    Return ``None`` when no explicit range was requested. Raise ``ValueError``
    when the range has no overlap with recent search; callers turn that into a
    placeholder instead of spending credits on current posts during a backtest.
    """
    if not start_date and not end_date:
        return None
    if not start_date or not end_date:
        raise ValueError("start_date and end_date must be provided together")

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)

    requested_start = datetime.strptime(start_date, "%Y-%m-%d").replace(
        tzinfo=timezone.utc
    )
    requested_end = datetime.strptime(end_date, "%Y-%m-%d").replace(
        tzinfo=timezone.utc
    ) + timedelta(days=1)
    # Keep a small margin inside the rolling boundary to avoid an API rejection
    # while X's recent-search index advances.
    earliest = current - timedelta(days=7) + timedelta(minutes=1)
    bounded_start = max(requested_start, earliest)
    bounded_end = min(requested_end, current)
    if bounded_start >= bounded_end:
        raise ValueError("requested dates fall outside X's rolling seven-day window")

    def _format(value: datetime) -> str:
        return value.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {"start_time": _format(bounded_start), "end_time": _format(bounded_end)}


def fetch_x_posts(
    ticker: str,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 30,
    timeout: float = 10.0,
    bearer_token: str | None = None,
) -> str:
    """Fetch recent public X posts for ``ticker`` using the official API.

    ``limit`` is clamped to X recent-search's supported 10–100 range. X bills
    reads per returned Post, so callers should keep this bounded. A token may
    be injected for tests; normal callers use ``X_BEARER_TOKEN``.
    """
    token = bearer_token or os.getenv("X_BEARER_TOKEN")
    if not token:
        return _unavailable("X_BEARER_TOKEN is not set")

    display_limit = max(1, min(int(limit), _MAX_RESULTS))
    max_results = max(_MIN_RESULTS, display_limit)
    params = {
        "query": _build_query(ticker),
        "max_results": max_results,
        "tweet.fields": "created_at,public_metrics,author_id",
        "expansions": "author_id",
        "user.fields": "username,verified",
    }
    try:
        time_params = _recent_time_params(start_date, end_date)
    except ValueError as exc:
        return _unavailable(str(exc))
    if time_params:
        params.update(time_params)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    try:
        response = requests.get(_API, params=params, headers=headers, timeout=timeout)
        if response.status_code == 401:
            return _unavailable("authentication failed; check X_BEARER_TOKEN")
        if response.status_code == 402:
            return _unavailable("X API credits are unavailable")
        if response.status_code == 403:
            return _unavailable("the X app is not permitted to use recent search")
        if response.status_code == 429:
            return _unavailable("X API rate limit exceeded")
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("X recent-search fetch failed for %s: %s", ticker, exc)
        return _unavailable(type(exc).__name__)

    if not isinstance(payload, dict):
        return _unavailable("unexpected API response")

    posts = payload.get("data") or []
    if not isinstance(posts, list) or not posts:
        return f"<no recent X posts found for ${_search_symbol(ticker)}>"

    users = {
        user.get("id"): user
        for user in (payload.get("includes") or {}).get("users", [])
        if isinstance(user, dict) and user.get("id")
    }
    lines = []
    total_likes = total_reposts = total_replies = total_quotes = 0
    for post in posts[:display_limit]:
        if not isinstance(post, dict):
            continue
        metrics = post.get("public_metrics") or {}
        likes = _metric(metrics, "like_count")
        reposts = _metric(metrics, "retweet_count")
        replies = _metric(metrics, "reply_count")
        quotes = _metric(metrics, "quote_count")
        total_likes += likes
        total_reposts += reposts
        total_replies += replies
        total_quotes += quotes

        author = users.get(post.get("author_id"), {})
        username = author.get("username") or "?"
        verified = " · verified" if author.get("verified") else ""
        created = post.get("created_at") or "?"
        body = " ".join(str(post.get("text") or "").split())
        if len(body) > 280:
            body = body[:280] + "…"
        lines.append(
            f"[{created} · @{username}{verified} · {likes} likes · {reposts} reposts · "
            f"{replies} replies · {quotes} quotes] {body}"
        )

    if not lines:
        return f"<no recent X posts found for ${_search_symbol(ticker)}>"

    summary = (
        f"X recent search for ${_search_symbol(ticker)}: {len(lines)} posts · "
        f"{total_likes} likes · {total_reposts} reposts · "
        f"{total_replies} replies · {total_quotes} quotes"
    )
    return summary + "\n\n" + "\n".join(lines)
