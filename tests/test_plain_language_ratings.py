"""Regression tests for plain-language portfolio decision vocabulary."""

import pytest

from tradingagents.agents.schemas import PortfolioRating
from tradingagents.agents.utils.rating import RATINGS_5_TIER, parse_rating


@pytest.mark.unit
def test_plain_language_rating_scale():
    assert RATINGS_5_TIER == (
        "Buy", "Cautious Buy", "Hold", "Reduce", "Sell"
    )
    assert [rating.value for rating in PortfolioRating] == list(RATINGS_5_TIER)


@pytest.mark.unit
def test_parser_handles_multiword_cautious_buy():
    assert parse_rating("**Rating**: Cautious Buy\nStart small.") == "Cautious Buy"


@pytest.mark.unit
def test_legacy_jargon_is_normalized():
    assert parse_rating("Rating: Cautious Buy\nBuild gradually.") == "Cautious Buy"
    assert parse_rating("Rating: Reduce\nTrim exposure.") == "Reduce"


@pytest.mark.unit
def test_enum_accepts_legacy_stored_values():
    assert PortfolioRating("Cautious Buy") is PortfolioRating.CAUTIOUS_BUY
    assert PortfolioRating("Reduce") is PortfolioRating.REDUCE
