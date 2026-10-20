from .base import Signal, Strategy
from .donchian import Donchian
from .ma_cross import MACross

STRATEGIES: dict[str, type[Strategy]] = {cls.name: cls for cls in (MACross, Donchian)}


def create_strategy(name: str, params: dict | None = None) -> Strategy:
    if name not in STRATEGIES:
        raise ValueError(f"未知策略 {name!r}，可选: {sorted(STRATEGIES)}")
    return STRATEGIES[name](**(params or {}))


__all__ = ["Signal", "Strategy", "STRATEGIES", "create_strategy"]
