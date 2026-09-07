from __future__ import annotations

from algo.paper.strategy import (
    EmaCrossStrategy,
    HoldStrategy,
    NiftyOptionOrbCallStrategy,
    NiftyOptionOrbPutStrategy,
    NiftyOptionOrbStrategy,
    SmaTrendStrategy,
    Strategy,
)
from algo.paper.equity_orb import (
    Nifty500GainerOrbCashStrategy,
    Nifty500GainerOrbStrategy,
    Nifty500LoserOrbCashStrategy,
    Nifty500LoserOrbStrategy,
)
from algo.paper.equity_orb_tier import (
    Nifty500TopGainerBrkStrategy,
    Nifty500TopLoserBrkStrategy,
)
from algo.paper.zen_credit import ZenCreditSpreadOvernightStrategy

_REGISTRY: dict[str, type[Strategy]] = {
    HoldStrategy.id: HoldStrategy,
    EmaCrossStrategy.id: EmaCrossStrategy,
    SmaTrendStrategy.id: SmaTrendStrategy,
    NiftyOptionOrbCallStrategy.id: NiftyOptionOrbCallStrategy,
    NiftyOptionOrbPutStrategy.id: NiftyOptionOrbPutStrategy,
    NiftyOptionOrbStrategy.id: NiftyOptionOrbStrategy,
    ZenCreditSpreadOvernightStrategy.id: ZenCreditSpreadOvernightStrategy,
    Nifty500GainerOrbStrategy.id: Nifty500GainerOrbStrategy,
    Nifty500LoserOrbStrategy.id: Nifty500LoserOrbStrategy,
    Nifty500GainerOrbCashStrategy.id: Nifty500GainerOrbCashStrategy,
    Nifty500LoserOrbCashStrategy.id: Nifty500LoserOrbCashStrategy,
    Nifty500TopGainerBrkStrategy.id: Nifty500TopGainerBrkStrategy,
    Nifty500TopLoserBrkStrategy.id: Nifty500TopLoserBrkStrategy,
}


def list_strategies() -> list[dict]:
    out = []
    for cls in _REGISTRY.values():
        sample = cls()
        out.append(
            {
                "id": cls.id,
                "name": cls.name,
                "description": cls.description,
                "default_params": sample.default_params(),
                "param_schema": sample.param_schema(),
                "asset_kinds": list(cls.asset_kinds),
            }
        )
    return out


def create_strategy(strategy_id: str, params: dict | None = None) -> Strategy:
    cls = _REGISTRY.get(strategy_id)
    if cls is None:
        known = ", ".join(sorted(_REGISTRY))
        raise ValueError(f"Unknown strategy '{strategy_id}'. Known: {known}")
    return cls(params=params)


def register_strategy(cls: type[Strategy]) -> type[Strategy]:
    _REGISTRY[cls.id] = cls
    return cls
