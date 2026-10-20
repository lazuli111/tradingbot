"""Position sizing and circuit breakers."""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from .config import RiskConfig


@dataclass
class OrderPlan:
    side: int            # 1 long, -1 short
    qty: float           # base-asset quantity
    stop_distance: float
    take_profit_distance: float | None


class RiskManager:
    """Sizes positions so a stop-out loses ~risk_per_trade of equity, and blocks
    new entries after a daily loss limit or a maximum drawdown is hit."""

    def __init__(self, cfg: RiskConfig, is_perp: bool, initial_equity: float):
        self.cfg = cfg
        self.is_perp = is_perp
        self.peak_equity = initial_equity
        self.day: str | None = None
        self.day_start_equity = initial_equity
        self.halted = False
        self.halt_reason: str | None = None

    def update(self, ts_ms: int, equity: float) -> None:
        """Call once per closed bar with mark-to-market equity."""
        day = pd.Timestamp(ts_ms, unit="ms", tz="UTC").strftime("%Y-%m-%d")
        if day != self.day:
            self.day = day
            self.day_start_equity = equity
        self.peak_equity = max(self.peak_equity, equity)
        drawdown = 1 - equity / self.peak_equity
        if not self.halted and drawdown >= self.cfg.max_drawdown:
            self.halted = True
            self.halt_reason = f"回撤 {drawdown:.1%} 达到上限 {self.cfg.max_drawdown:.0%}"

    def can_open(self, equity: float) -> tuple[bool, str]:
        if self.halted:
            return False, f"已熔断: {self.halt_reason}"
        if equity <= self.day_start_equity * (1 - self.cfg.daily_loss_limit):
            return False, f"当日亏损达到 {self.cfg.daily_loss_limit:.0%}，今天不再开仓"
        return True, ""

    def plan(self, side: int, equity: float, price: float, atr: float) -> OrderPlan | None:
        if side == 0 or equity <= 0 or price <= 0 or not math.isfinite(atr) or atr <= 0:
            return None
        stop_distance = atr * self.cfg.stop_atr_mult
        qty_by_risk = equity * self.cfg.risk_per_trade / stop_distance
        max_notional = equity * (self.cfg.leverage if self.is_perp else self.cfg.max_position_pct)
        qty = min(qty_by_risk, max_notional / price)
        tp_mult = self.cfg.take_profit_atr_mult
        return OrderPlan(side, qty, stop_distance, atr * tp_mult if tp_mult else None)

    def state(self) -> dict:
        return {"peak_equity": self.peak_equity, "day": self.day,
                "day_start_equity": self.day_start_equity,
                "halted": self.halted, "halt_reason": self.halt_reason}

    def restore(self, state: dict) -> None:
        for key, value in state.items():
            setattr(self, key, value)
