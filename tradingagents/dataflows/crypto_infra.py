"""Shared bounded infrastructure for public crypto data providers."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class HTTPPolicy:
    allowed_hosts: frozenset[str]
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 20.0
    max_retries: int = 2
    backoff_seconds: float = 0.5
    max_response_bytes: int = 2_000_000

    def __post_init__(self) -> None:
        if not self.allowed_hosts:
            raise ValueError("allowed_hosts cannot be empty")
        if self.connect_timeout_seconds <= 0 or self.read_timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")
        if not 0 <= self.max_retries <= 3:
            raise ValueError("max_retries must be between 0 and 3")
        if not 0 < self.max_response_bytes <= 5_000_000:
            raise ValueError("max_response_bytes must be bounded")

    @property
    def timeout(self) -> tuple[float, float]:
        return (self.connect_timeout_seconds, self.read_timeout_seconds)

    def validate_url(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "https":
            raise ValueError("provider URLs must use HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("provider URLs must not contain credentials")
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if hostname not in {host.lower().rstrip(".") for host in self.allowed_hosts}:
            raise ValueError("provider host is not allowlisted")


class BoundedTTLCache:
    """Small process-local LRU cache with deterministic expiry."""

    def __init__(
        self,
        max_entries: int,
        ttl: timedelta = timedelta(minutes=2),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        self.max_entries = max_entries
        self.ttl = ttl
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._items: OrderedDict[Hashable, tuple[datetime, Any]] = OrderedDict()

    def get(self, key: Hashable) -> Any | None:
        item = self._items.get(key)
        if item is None:
            return None
        inserted_at, value = item
        if self.clock() - inserted_at >= self.ttl:
            del self._items[key]
            return None
        self._items.move_to_end(key)
        return value

    def set(self, key: Hashable, value: Any) -> None:
        self._items[key] = (self.clock(), value)
        self._items.move_to_end(key)
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()
