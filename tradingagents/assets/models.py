"""Asset-neutral request, identity, and resolution errors."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class AssetType(str, Enum):
    STOCK = "stock"
    CRYPTO = "crypto"


class MarketType(str, Enum):
    EQUITY = "equity"
    SPOT = "spot"
    PERPETUAL = "perpetual"


class AssetResolutionError(ValueError):
    """Base error for requests that cannot be resolved safely."""


class AssetValidationError(AssetResolutionError):
    """Raised when explicit identity fields are invalid or inconsistent."""


class AmbiguousAssetError(AssetResolutionError):
    """Raised when resolving would require guessing among possible assets."""


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


@dataclass(frozen=True, slots=True)
class AssetRequest:
    asset_type: AssetType | str | None = None
    symbol: str | None = None
    name: str | None = None
    chain: str | None = None
    contract_address: str | None = None
    coingecko_id: str | None = None
    geckoterminal_network: str | None = None
    geckoterminal_pool_address: str | None = None
    venue: str | None = None
    instrument: str | None = None
    market_type: MarketType | str | None = None
    quote_currency: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedAsset:
    canonical_id: str
    asset_type: AssetType
    display_symbol: str
    market_type: MarketType
    quote_currency: str
    display_name: str | None = None
    chain: str | None = None
    contract_address: str | None = None
    coingecko_id: str | None = None
    geckoterminal_network: str | None = None
    geckoterminal_pool_address: str | None = None
    venue: str | None = None
    instrument: str | None = None
    provider_ids: dict[str, str] = field(default_factory=dict)
    resolution_confidence: float = 1.0
    warnings: tuple[str, ...] = ()

    def to_state_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation suitable for LangGraph state."""
        value = asdict(self)
        value["asset_type"] = _enum_value(self.asset_type)
        value["market_type"] = _enum_value(self.market_type)
        value["warnings"] = list(self.warnings)
        return value
