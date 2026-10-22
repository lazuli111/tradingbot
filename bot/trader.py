"""Decision core shared by the backtester and the live runner.

Bar lifecycle (the live runner calls the same methods):
    on_bar_open   -> funding, then execute the order decided at the previous close
    check_exits   -> stop / take-profit / liquidation against the bar's range
    on_bar_close  -> mark equity, update circuit breakers, read the new signal

If executing raises (exchange error), `pending` is kept: the live runner reconciles
the position with the exchange and calls `execute_pending` again. `_execute` works
from the broker's current position, so the retry never repeats an order that went through.
"""
from __future__ import annotations

import logging
import math
from typing import Callable

from .broker import Broker, Fill
from .risk import RiskManager

log = logging.getLogger(__name__)


class Trader:
    def __init__(self, risk: RiskManager, broker: Broker, allow_short: bool,
                 on_fill: Callable[[Fill], None] | None = None):
        self.risk = risk
        self.broker = broker
        self.allow_short = allow_short
        self.on_fill = on_fill
        self.last_signal = 0                       # last direction we acted on
        self.pending: tuple[int, float] | None = None  # (target, atr) for the next open
        self.last_atr: float | None = None
        self._blocked_reason: str | None = None

    # ---- bar lifecycle -------------------------------------------------
    def on_bar_open(self, ts: int, price: float, funding_rate: float = 0.0) -> None:
        if funding_rate:
            self.broker.apply_funding(funding_rate, price, ts)
        self.execute_pending(ts, price)

    def execute_pending(self, ts: int, price: float) -> None:
        if self.pending is None:
            return
        if self.broker.unresolved:
            log.warning("订单 %s 状态未知，核对之前暂不执行信号", self.broker.unresolved["id"])
            return
        target, atr = self.pending
        self._execute(target, atr, ts, price)
        self.pending = None  # only once _execute got through without an exception

    def reconcile(self, ts: int, price: float) -> None:
        """Take the exchange's word for the position (see Broker.reconcile)."""
        self._emit(self.broker.reconcile(price, ts))
        self._protect()

    def check_exits(self, ts: int, open_: float, high: float, low: float) -> None:
        pos = self.broker.position
        if pos is None:
            return
        liq, stop, tp = pos.liq_price, pos.stop, pos.take_profit
        if pos.side == 1:
            liquidated = liq is not None and (open_ <= liq or ((stop is None or stop < liq) and low <= liq))
            stop_hit, stop_px = stop is not None and low <= stop, min(open_, stop or 0)
            tp_hit, tp_px = tp is not None and high >= tp, max(open_, tp or 0)
        else:
            liquidated = liq is not None and (open_ >= liq or ((stop is None or stop > liq) and high >= liq))
            stop_hit, stop_px = stop is not None and high >= stop, max(open_, stop or 0)
            tp_hit, tp_px = tp is not None and low <= tp, min(open_, tp or math.inf)
        # when stop and take-profit are both inside one bar, assume the stop came first
        if liquidated:
            self._emit(self.broker.liquidate(ts))
        elif stop_hit:
            self._emit(self.broker.close(stop_px, ts, "stop_loss"))
        elif tp_hit:
            self._emit(self.broker.close(tp_px, ts, "take_profit"))

    def on_bar_close(self, ts: int, close: float, signal: int, atr: float) -> float:
        self.last_atr = atr
        self._protect()
        equity = self.broker.equity(close)
        was_halted = self.risk.halted
        self.risk.update(ts, equity)
        if self.risk.halted and not was_halted:
            log.warning("触发熔断: %s，平仓并停止开新仓", self.risk.halt_reason)
            self._emit(self.broker.close(close, ts, "circuit_breaker"))
            equity = self.broker.equity(close)
        if (signal < 0 and not self.allow_short) or self.risk.halted:
            signal = 0
        # also drops a pending retry the new signal no longer wants
        self.pending = (signal, atr) if signal != self.last_signal else None
        return equity

    # ---- internals -----------------------------------------------------
    def _execute(self, target: int, atr: float, ts: int, price: float) -> None:
        pos = self.broker.position
        current = pos.side if pos else 0
        if current == target:
            self.last_signal = target
            return
        if current != 0:
            fill = self.broker.close(price, ts, "signal")
            self._emit(fill)
            if fill is None:  # couldn't close (e.g. below exchange minimum) — retry next bar
                return
        if target != 0:
            equity = self.broker.equity(price)
            ok, why = self.risk.can_open(equity)
            if not ok:
                if why != self._blocked_reason:
                    log.info("不开仓: %s", why)
                    self._blocked_reason = why
                return  # last_signal unchanged, so we retry on the next bar
            self._blocked_reason = None
            plan = self.risk.plan(target, equity, price, atr)
            fill = self.broker.open(target, plan.qty, price, ts, "signal") if plan else None
            if fill is None:
                return
            pos = self.broker.position
            pos.stop = fill.price - target * plan.stop_distance
            if plan.take_profit_distance:
                pos.take_profit = fill.price + target * plan.take_profit_distance
            self._emit(fill)
        self.last_signal = target

    def _protect(self) -> None:
        """A position the bot didn't open itself (adopted from the exchange after an
        order error or a restart) has no soft stop: give it one from the latest ATR."""
        pos = self.broker.position
        atr = self.last_atr
        if pos is None or pos.stop is not None or not atr or not math.isfinite(atr):
            return
        cfg = self.risk.cfg
        pos.stop = pos.entry - pos.side * atr * cfg.stop_atr_mult
        if cfg.take_profit_atr_mult:
            pos.take_profit = pos.entry + pos.side * atr * cfg.take_profit_atr_mult
        log.warning("持仓没有止损（从交易所同步而来），按 ATR 补设止损 %.2f", pos.stop)

    def _emit(self, fill: Fill | None) -> None:
        if fill is not None and self.on_fill:
            self.on_fill(fill)

    def state(self) -> dict:
        return {"last_signal": self.last_signal, "risk": self.risk.state(),
                "broker": self.broker.state()}

    def restore(self, state: dict) -> None:
        self.last_signal = state["last_signal"]
        self.risk.restore(state["risk"])
        self.broker.restore(state["broker"])
