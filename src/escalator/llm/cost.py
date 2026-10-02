"""Cost meter: normalized usage -> exact nominal USD, from configs/prices.yaml.

`usd` is the nominal cost of a call, recomputed from stored usage on a cache hit.
Actual spend in a run is the sum of usd over non-hits.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

from escalator.datasets.config import REPO_ROOT
from escalator.llm.errors import PriceMissing, PriceTableError, UnexpectedCacheUsage
from escalator.llm.types import Usage

DEFAULT_PRICES = REPO_ROOT / "configs" / "prices.yaml"
MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class Price:
    input: Decimal  # USD per million tokens
    output: Decimal


@dataclass(frozen=True)
class PriceTable:
    retrieved_on: dt.date
    source: str
    sha256: str  # of the file bytes; goes into RunMeta
    models: dict[str, Price]

    @classmethod
    def load(cls, path: Path = DEFAULT_PRICES) -> PriceTable:
        return cls.parse(path.read_bytes(), where=str(path))

    @classmethod
    def parse(cls, data: bytes, where: str = "prices.yaml") -> PriceTable:
        raw: object = yaml.safe_load(data)
        if not isinstance(raw, dict):
            raise PriceTableError(f"{where}: expected a mapping")
        retrieved_on, source, unit, models = (raw.get(k) for k in ("retrieved_on", "source", "unit", "models"))
        if not isinstance(retrieved_on, dt.date):
            raise PriceTableError(f"{where}: retrieved_on must be a date")
        if not isinstance(source, str):
            raise PriceTableError(f"{where}: source must be a string")
        if unit != "usd_per_million_tokens":
            raise PriceTableError(f"{where}: unit must be usd_per_million_tokens")
        if not isinstance(models, dict) or not models:
            raise PriceTableError(f"{where}: models must be a non-empty mapping")
        table: dict[str, Price] = {}
        for model, entry in models.items():
            if not isinstance(model, str) or not isinstance(entry, dict) or set(entry) != {"input", "output"}:
                raise PriceTableError(f"{where}: models.{model} must be {{input, output}}")
            table[model] = Price(_price(entry["input"], f"{where}: models.{model}.input"),
                                 _price(entry["output"], f"{where}: models.{model}.output"))
        return cls(retrieved_on, source, hashlib.sha256(data).hexdigest(), table)

    def price(self, model: str) -> Price:
        try:
            return self.models[model]
        except KeyError:
            raise PriceMissing(f"no price for model {model!r}") from None


def _price(value: object, where: str) -> Decimal:
    # A YAML float is refused: Decimal(0.3) != Decimal("0.3").
    if not isinstance(value, str):
        raise PriceTableError(f"{where}: price must be a quoted string, got {type(value).__name__}")
    try:
        d = Decimal(value)
    except InvalidOperation:
        raise PriceTableError(f"{where}: {value!r} is not a decimal") from None
    if not d.is_finite() or d < 0:
        raise PriceTableError(f"{where}: {value!r} must be a finite non-negative decimal")
    return d


def check_no_cache_usage(usage: Usage) -> None:
    if usage.cache_read or usage.cache_write:
        raise UnexpectedCacheUsage(f"cache_read={usage.cache_read}, cache_write={usage.cache_write}")


def usd(usage: Usage, model: str, table: PriceTable) -> Decimal:
    """Exact nominal cost. Unreported token counts (None) cost nothing but stay None in the trace."""
    check_no_cache_usage(usage)
    price = table.price(model)
    return ((usage.uncached_in or 0) * price.input + (usage.out or 0) * price.output) / MILLION
