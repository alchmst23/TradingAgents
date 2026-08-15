"""Shared 5-tier rating vocabulary and a deterministic heuristic parser.

The same five-tier scale (Buy, Cautious Buy, Hold, Reduce, Sell) is used by:
- The Research Manager (investment plan recommendation)
- The Portfolio Manager (final position decision)
- The signal processor (rating extracted for downstream consumers)
- The memory log (rating tag stored alongside each decision entry)

Centralising it here avoids drift between those call sites.
"""

from __future__ import annotations

import re

# Canonical, ordered 5-tier scale (most bullish to most bearish).
RATINGS_5_TIER: tuple[str, ...] = (
    "Buy", "Cautious Buy", "Hold", "Reduce", "Sell",
)

# Historical reports and memory entries may still contain the former jargon.
_LEGACY_RATING_ALIASES = {
    "overweight": "Cautious Buy",
    "underweight": "Reduce",
}
_RATING_ALIASES = {
    **{rating.lower(): rating for rating in RATINGS_5_TIER},
    **_LEGACY_RATING_ALIASES,
}
_RATING_LABEL_RE = re.compile(r"rating.*?[:\-][\s*]*(.+?)(?:\*|$)", re.IGNORECASE)
_RATING_WORD_RE = re.compile(
    r"\b(cautious\s+buy|overweight|underweight|reduce|hold|sell|buy)\b",
    re.IGNORECASE,
)

def parse_rating(text: str, default: str = "Hold") -> str:
    """Extract a plain-language rating, normalizing historical jargon."""
    for line in text.splitlines():
        match = _RATING_LABEL_RE.search(line)
        if match:
            candidate = re.sub(r"[*_`]", "", match.group(1)).strip().lower()
            for phrase, canonical in _RATING_ALIASES.items():
                if candidate.startswith(phrase):
                    return canonical

    match = _RATING_WORD_RE.search(text)
    if match:
        return _RATING_ALIASES[re.sub(r"\s+", " ", match.group(1).lower())]
    return default
