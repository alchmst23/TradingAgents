"""Freshness and cross-provider conflict evaluation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from itertools import combinations
from typing import Mapping

from tradingagents.evidence import Observation


@dataclass(frozen=True, slots=True)
class FreshnessPolicy:
    max_age_by_type: Mapping[str, timedelta]
    fallback_max_age: timedelta = timedelta(minutes=15)

    @classmethod
    def default(cls) -> FreshnessPolicy:
        return cls(
            max_age_by_type={
                "market": timedelta(minutes=5),
                "dex": timedelta(minutes=5),
                "derivatives": timedelta(minutes=5),
                "sentiment": timedelta(minutes=30),
                "metadata": timedelta(days=1),
            }
        )

    def max_age(self, data_type: str) -> timedelta:
        return self.max_age_by_type.get(data_type, self.fallback_max_age)


@dataclass(frozen=True, slots=True)
class PriceConflict:
    providers: tuple[str, str]
    quote_currency: str | None
    relative_difference: float
    prices: tuple[float, float]


def evaluate_freshness(
    observation: Observation,
    now: datetime,
    policy: FreshnessPolicy,
) -> Observation:
    warnings = list(observation.warnings)
    if observation.source_timestamp is None:
        warnings.append("missing source timestamp")
        return replace(observation, stale=True, warnings=tuple(warnings))
    age = now - observation.source_timestamp
    stale = age < timedelta(0) or age > policy.max_age(observation.data_type)
    if age < timedelta(0):
        warnings.append("source timestamp is in the future")
    elif stale:
        warnings.append(f"source data exceeds {policy.max_age(observation.data_type)} freshness limit")
    return replace(observation, stale=stale, warnings=tuple(warnings))


def detect_price_conflicts(
    observations: tuple[Observation, ...],
    relative_threshold: float = 0.05,
) -> tuple[PriceConflict, ...]:
    candidates: list[tuple[Observation, float]] = []
    for observation in observations:
        price = observation.payload.get("price")
        if observation.stale or isinstance(price, bool) or not isinstance(price, int | float):
            continue
        if price <= 0:
            continue
        candidates.append((observation, float(price)))

    conflicts: list[PriceConflict] = []
    for (left, left_price), (right, right_price) in combinations(candidates, 2):
        if left.provider == right.provider or left.quote_currency != right.quote_currency:
            continue
        relative = abs(left_price - right_price) / min(left_price, right_price)
        if relative > relative_threshold:
            conflicts.append(
                PriceConflict(
                    providers=(left.provider, right.provider),
                    quote_currency=left.quote_currency,
                    relative_difference=relative,
                    prices=(left_price, right_price),
                )
            )
    return tuple(conflicts)
