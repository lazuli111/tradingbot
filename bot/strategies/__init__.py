from .base import Signal, Strategy
from .bb_breakout import BBBreakout
from .bb_reversion import BBReversion
from .donchian import Donchian
from .dual_thrust import DualThrust
from .ma_cross import MACross
from .rsi2 import RSI2
from .vol_system import VolSystem

STRATEGIES: dict[str, type[Strategy]] = {
    cls.name: cls for cls in (MACross, Donchian, RSI2, BBReversion, BBBreakout, DualThrust, VolSystem)}


def create_strategy(name: str, params: dict | None = None) -> Strategy:
    if name not in STRATEGIES:
        raise ValueError(f"未知策略 {name!r}，可选: {sorted(STRATEGIES)}")
    return STRATEGIES[name](**(params or {}))


__all__ = ["Signal", "Strategy", "STRATEGIES", "create_strategy"]
