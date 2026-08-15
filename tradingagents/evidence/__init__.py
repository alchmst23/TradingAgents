"""Normalized, immutable evidence contracts for analysis providers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from tradingagents.assets import MarketType, ResolvedAsset

_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s&]+"),
    re.compile(r"(?i)((?:api[_-]?key|x_cg_demo_api_key|token|secret)=)[^\s&]+"),
)


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


def _value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _freeze(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


def _sanitize(message: str) -> str:
    clean = message
    for pattern in _SECRET_PATTERNS:
        clean = pattern.sub(r"\1[REDACTED]", clean)
    return clean[:500]


@dataclass(frozen=True, slots=True)
class Observation:
    provider: str
    canonical_asset_id: str
    data_type: str
    observed_at: datetime
    payload: Mapping[str, Any]
    source_timestamp: datetime | None = None
    quote_currency: str | None = None
    market_type: MarketType | str | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)
    stale: bool = False
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.canonical_asset_id.strip() or not self.data_type.strip():
            raise ValueError("provider, canonical_asset_id, and data_type are required")
        _utc(self.observed_at, "observed_at")
        if self.source_timestamp is not None:
            _utc(self.source_timestamp, "source_timestamp")
        object.__setattr__(self, "payload", _freeze(self.payload))
        object.__setattr__(self, "provenance", _freeze(self.provenance))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "canonical_asset_id": self.canonical_asset_id,
            "data_type": self.data_type,
            "observed_at": self.observed_at.isoformat(),
            "source_timestamp": self.source_timestamp.isoformat() if self.source_timestamp else None,
            "quote_currency": self.quote_currency,
            "market_type": _value(self.market_type),
            "payload": _plain(self.payload),
            "provenance": _plain(self.provenance),
            "stale": self.stale,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class ProviderError:
    provider: str
    code: str
    message: str
    observed_at: datetime
    retryable: bool = False
    data_type: str | None = None

    def __post_init__(self) -> None:
        _utc(self.observed_at, "observed_at")
        object.__setattr__(self, "message", _sanitize(self.message))

    @classmethod
    def sanitized(cls, **kwargs: Any) -> ProviderError:
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "code": self.code,
            "message": self.message,
            "observed_at": self.observed_at.isoformat(),
            "retryable": self.retryable,
            "data_type": self.data_type,
        }


@dataclass(frozen=True, slots=True)
class CryptoEvidencePacket:
    asset: ResolvedAsset
    observations: tuple[Observation, ...] = ()
    provider_errors: tuple[ProviderError, ...] = ()
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completeness: float = 0.0
    freshness_summary: Mapping[str, str] = field(default_factory=dict)
    conflicts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _utc(self.generated_at, "generated_at")
        if not 0.0 <= self.completeness <= 1.0:
            raise ValueError("completeness must be between 0 and 1")
        for observation in self.observations:
            if observation.canonical_asset_id != self.asset.canonical_id:
                raise ValueError("observation does not match packet asset")
        object.__setattr__(self, "observations", tuple(self.observations))
        object.__setattr__(self, "provider_errors", tuple(self.provider_errors))
        object.__setattr__(self, "freshness_summary", _freeze(self.freshness_summary))
        object.__setattr__(self, "conflicts", tuple(self.conflicts))

    @property
    def grouped_observations(self) -> dict[str, tuple[Observation, ...]]:
        grouped: dict[str, list[Observation]] = {}
        for observation in self.observations:
            grouped.setdefault(observation.data_type, []).append(observation)
        return {key: tuple(value) for key, value in grouped.items()}

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset.to_state_dict(),
            "observations": [item.to_dict() for item in self.observations],
            "provider_errors": [item.to_dict() for item in self.provider_errors],
            "generated_at": self.generated_at.isoformat(),
            "completeness": self.completeness,
            "freshness_summary": dict(self.freshness_summary),
            "conflicts": list(self.conflicts),
        }
