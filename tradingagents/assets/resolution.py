"""Deterministic asset identity resolution with no ticker guessing."""

from __future__ import annotations

import re

from .models import (
    AmbiguousAssetError,
    AssetRequest,
    AssetType,
    AssetValidationError,
    MarketType,
    ResolvedAsset,
)

_MAJOR_ASSETS = {
    "BTC": ("bitcoin", "Bitcoin"),
    "BITCOIN": ("bitcoin", "Bitcoin"),
    "ETH": ("ethereum", "Ethereum"),
    "ETHEREUM": ("ethereum", "Ethereum"),
}
_EVM_CHAINS = {"ethereum", "base", "arbitrum", "optimism", "polygon", "bsc", "avalanche"}
_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _coerce_asset_type(value: AssetType | str | None) -> AssetType | None:
    if value is None:
        return None
    try:
        return AssetType(getattr(value, "value", value).lower())
    except (AttributeError, ValueError) as exc:
        raise AssetValidationError(f"Unsupported asset type: {value!r}") from exc


def _coerce_market_type(value: MarketType | str | None) -> MarketType | None:
    if value is None:
        return None
    try:
        return MarketType(getattr(value, "value", value).lower())
    except (AttributeError, ValueError) as exc:
        raise AssetValidationError(f"Unsupported market type: {value!r}") from exc


def _normal(value: str | None) -> str | None:
    value = value.strip() if value else None
    return value or None


def _validate_contract(chain: str, address: str) -> str:
    if chain in _EVM_CHAINS:
        if not _EVM_ADDRESS.fullmatch(address):
            raise AssetValidationError(
                f"Invalid Ethereum contract address for EVM-compatible chain {chain!r}"
            )
        return "0x" + address[2:].lower()
    raise AssetValidationError(
        f"Contract validation is not implemented for chain {chain!r}; refusing generic validation"
    )


def resolve_asset(request: AssetRequest) -> ResolvedAsset:
    """Resolve explicit identity fields into a canonical, provider-neutral asset.

    The resolver is intentionally local and deterministic. Provider-assisted
    lookup belongs in a later layer and must return exactly one constrained
    candidate before this function is called.
    """
    symbol = (_normal(request.symbol) or "").upper()
    name = _normal(request.name)
    chain = (_normal(request.chain) or "").lower() or None
    contract = _normal(request.contract_address)
    coingecko_id = (_normal(request.coingecko_id) or "").lower() or None
    venue = (_normal(request.venue) or "").lower() or None
    instrument = (_normal(request.instrument) or "").upper() or None
    quote = (_normal(request.quote_currency) or "").upper() or None
    asset_type = _coerce_asset_type(request.asset_type)
    market_type = _coerce_market_type(request.market_type)

    if bool(chain) != bool(contract):
        raise AssetValidationError("chain and contract_address must be supplied together")

    if chain and contract:
        if asset_type not in (None, AssetType.CRYPTO):
            raise AssetValidationError("chain/contract identity requires asset_type='crypto'")
        contract = _validate_contract(chain, contract)
        return ResolvedAsset(
            canonical_id=f"crypto:{chain}:{contract}",
            asset_type=AssetType.CRYPTO,
            display_symbol=symbol or contract[:10],
            display_name=name,
            market_type=market_type or MarketType.SPOT,
            quote_currency=quote or "USD",
            chain=chain,
            contract_address=contract,
            coingecko_id=coingecko_id,
            geckoterminal_network=_normal(request.geckoterminal_network),
            geckoterminal_pool_address=_normal(request.geckoterminal_pool_address),
            provider_ids={chain: contract},
        )

    if venue or instrument or market_type is MarketType.PERPETUAL:
        if not (venue and instrument and market_type):
            raise AssetValidationError(
                "venue, instrument, and market_type are all required for venue identity"
            )
        if market_type is MarketType.EQUITY:
            raise AssetValidationError("venue crypto identity cannot use equity market_type")
        quote = quote or ("USDC" if venue == "hyperliquid" else "USD")
        return ResolvedAsset(
            canonical_id=f"crypto:{venue}:{instrument}:{market_type.value}:{quote}",
            asset_type=AssetType.CRYPTO,
            display_symbol=symbol or instrument,
            display_name=name,
            market_type=market_type,
            quote_currency=quote,
            venue=venue,
            instrument=instrument,
            provider_ids={venue: instrument},
        )

    if coingecko_id:
        if asset_type not in (None, AssetType.CRYPTO):
            raise AssetValidationError("CoinGecko identity requires asset_type='crypto'")
        return ResolvedAsset(
            canonical_id=f"crypto:coingecko:{coingecko_id}:{(market_type or MarketType.SPOT).value}:{quote or 'USD'}",
            asset_type=AssetType.CRYPTO,
            display_symbol=symbol or coingecko_id.upper(),
            display_name=name,
            market_type=market_type or MarketType.SPOT,
            quote_currency=quote or "USD",
            coingecko_id=coingecko_id,
            provider_ids={"coingecko": coingecko_id},
        )

    pair_base = None
    pair_quote = None
    if "-" in symbol:
        parts = symbol.rsplit("-", 1)
        if len(parts) == 2 and parts[1] in {"USD", "USDT", "USDC"}:
            pair_base, pair_quote = parts
    major_key = pair_base or symbol
    if major_key in _MAJOR_ASSETS and (asset_type in (None, AssetType.CRYPTO)):
        cg_id, display_name = _MAJOR_ASSETS[major_key]
        quote = quote or pair_quote or "USD"
        return ResolvedAsset(
            canonical_id=f"crypto:coingecko:{cg_id}:spot:{quote}",
            asset_type=AssetType.CRYPTO,
            display_symbol=symbol or major_key,
            display_name=name or display_name,
            market_type=MarketType.SPOT,
            quote_currency=quote,
            coingecko_id=cg_id,
            provider_ids={"coingecko": cg_id},
            resolution_confidence=1.0,
        )

    if asset_type is AssetType.CRYPTO:
        raise AmbiguousAssetError(
            f"Crypto symbol {symbol or name!r} needs a canonical identifier: "
            "chain+contract_address, coingecko_id, or venue+instrument+market_type"
        )

    if not symbol:
        raise AssetValidationError("symbol is required for stock identity")
    return ResolvedAsset(
        canonical_id=f"equity:{symbol}",
        asset_type=AssetType.STOCK,
        display_symbol=symbol,
        display_name=name,
        market_type=MarketType.EQUITY,
        quote_currency=quote or "USD",
        provider_ids={"ticker": symbol},
    )
