"""Cost accounting.

Cost comes from exactly one of: the provider's own usage metadata (``reported_cost_usd``),
an explicit price table with source/update metadata, or it is ``UNKNOWN``. Astrometron
never invents prices. The price table used is embedded in the receipt, so historical
cost keeps the assumptions it was computed under.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .canonical import digest


class PricingError(ValueError):
    pass


@dataclass(frozen=True)
class PriceTable:
    source: str
    updated_at: str
    #: ``provider/model`` -> {"input_per_mtok": float, "output_per_mtok": float}
    models: dict[str, dict[str, float]] = field(default_factory=dict)
    currency: str = "USD"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "updated_at": self.updated_at,
            "currency": self.currency,
            "models": self.models,
        }

    @property
    def digest(self) -> str:
        return digest(self.to_dict())

    @classmethod
    def from_mapping(cls, m: Mapping[str, Any]) -> PriceTable:
        if not m.get("source") or not m.get("updated_at"):
            raise PricingError("price table needs 'source' and 'updated_at'")
        if str(m.get("currency", "USD")) != "USD":
            raise PricingError("only USD price tables are supported")
        models: dict[str, dict[str, float]] = {}
        for key, p in (m.get("models") or {}).items():
            try:
                models[str(key)] = {
                    "input_per_mtok": float(p["input_per_mtok"]),
                    "output_per_mtok": float(p["output_per_mtok"]),
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise PricingError(f"bad price entry for {key}: {exc}") from exc
        return cls(str(m["source"]), str(m["updated_at"]), models)


def load_price_table(spec: Any, base_dir: Path) -> PriceTable | None:
    if spec is None:
        return None
    if isinstance(spec, str):
        p = Path(spec)
        p = p if p.is_absolute() else base_dir / p
        return PriceTable.from_mapping(yaml.safe_load(p.read_text(encoding="utf-8")))
    if isinstance(spec, Mapping):
        return PriceTable.from_mapping(spec)
    raise PricingError("pricing must be a path or a mapping")


def case_cost(
    model_key: str, usages: Sequence[Mapping[str, Any]], table: PriceTable | None
) -> tuple[float | None, str]:
    """Return (cost_usd or None, basis) where basis is provider|price_table|UNKNOWN|none."""
    if not usages:
        return 0.0, "none"
    reported = [u.get("reported_cost_usd") for u in usages]
    if all(isinstance(r, (int, float)) for r in reported):
        return float(sum(r for r in reported if r is not None)), "provider"
    if table is None or model_key not in table.models:
        return None, "UNKNOWN"
    price = table.models[model_key]
    total = 0.0
    for u in usages:
        it, ot = u.get("input_tokens"), u.get("output_tokens")
        if it is None or ot is None:
            return None, "UNKNOWN"
        total += it * price["input_per_mtok"] / 1e6 + ot * price["output_per_mtok"] / 1e6
    return total, "price_table"
