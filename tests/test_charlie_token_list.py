import pytest

from tradingagents.assets import AssetRequest, resolve_asset


# Charlie's Buzz risk-compliance list, message
# 2ee7e27128b5f81af26af032588fe203b45ef45ac168716d7acc05e63ea9be66.
REAL_TOKENS = {
    "BTC": "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",
    "ETH": "0x4200000000000000000000000000000000000006",
    "SOL": "0x311935cd80b76769bf2ecc9d8ab7635b2139cf82",
    "LINK": "0x88fb150bdc53a65fe94dea0c9ba0a6daf8c6e196",
    "WIF": "0x7f6f6720a73c0f54f95ab343d7efeb1fa991f4f7",
    "USDC": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "USDT": "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2",
    "DAI": "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",
}
HELD_TOKENS = {
    "BNKR": "0x22af33fe49fd1fa80c7149773dde5890d3c76f3b",
    "VIRTUAL": "0x0b3e328455c4059eeb9e3f84b5543f74e24e7e1b",
    "DRB": "0x3ec2156d4c0a9cbdab4a016633b7bcf6a8d68ea2",
    "TIBBIR": "0xa4a2e2ca3fbfe21aed83471d28b6f65a233c6e00",
    "ZORA": "0x1111111111166b7fe7bd91427724b487980afc69",
}


@pytest.mark.parametrize(("symbol", "address"), (REAL_TOKENS | HELD_TOKENS).items())
def test_charlie_base_token_list_resolves_by_chain_and_contract(symbol, address):
    asset = resolve_asset(
        AssetRequest(
            asset_type="crypto",
            symbol=symbol,
            chain="base",
            contract_address=address,
        )
    )
    assert asset.canonical_id == f"crypto:base:{address}"
    assert asset.display_symbol == symbol
    assert asset.contract_address == address
