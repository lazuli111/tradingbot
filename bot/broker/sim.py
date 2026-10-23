"""Local order-fill simulator used for backtests and paper trading."""
from __future__ import annotations

from .base import Broker, BrokerStats, Fill, Position

MIN_NOTIONAL = 5.0  # ignore dust orders, like real exchanges do


class SimBroker(Broker):
    """Single-symbol simulated account.

    spot: `cash` is the quote balance; buying spends it, only longs allowed.
    perp: `cash` is the wallet balance (isolated margin, linear USDT contract);
          PnL is realized on close, funding is charged on the open notional, and
          the position is liquidated (losing its margin) at the liquidation price.
    """

    def __init__(self, is_perp: bool, initial_equity: float, fee_rate: float,
                 slippage: float = 0.0, leverage: float = 1.0,
                 maintenance_margin_rate: float = 0.004):
        self.is_perp = is_perp
        self.cash = initial_equity
        self.fee_rate = fee_rate
        self.slippage = slippage
        self.leverage = leverage
        self.mmr = maintenance_margin_rate
        self.position: Position | None = None
        self.stats = BrokerStats()

    def equity(self, price: float) -> float:
        pos = self.position
        if pos is None:
            return self.cash
        if self.is_perp:
            return self.cash + pos.side * pos.qty * (price - pos.entry)
        return self.cash + pos.qty * price

    def liquidation_price(self, side: int, entry: float) -> float:
        # Isolated margin, fees ignored: the position is closed when the loss
        # eats the initial margin down to the maintenance margin.
        return entry * (1 - side * (1 / self.leverage - self.mmr))

    def open(self, side: int, qty: float, price: float, ts: int, reason: str) -> Fill | None:
        if self.position is not None:
            raise RuntimeError("已有持仓，请先平仓")
        if side == -1 and not self.is_perp:
            raise ValueError("现货不能做空")
        fill_price = price * (1 + side * self.slippage)
        # cap the order by what the account can afford, fee included
        cost_per_unit = fill_price * ((1 / self.leverage if self.is_perp else 1) + self.fee_rate)
        qty = min(qty, self.cash / cost_per_unit)
        notional = qty * fill_price
        if notional < MIN_NOTIONAL:
            return None
        fee = notional * self.fee_rate
        margin = notional / self.leverage if self.is_perp else 0.0
        self.cash -= fee if self.is_perp else notional + fee
        self.stats.fees += fee
        self.position = Position(
            side=side, qty=qty, entry=fill_price, opened_at=ts, entry_fee=fee, margin=margin,
            liq_price=self.liquidation_price(side, fill_price) if self.is_perp else None)
        return Fill(ts, "buy" if side == 1 else "sell", qty, fill_price, fee, reason, ref_price=price)

    def close(self, price: float, ts: int, reason: str) -> Fill | None:
        pos = self.position
        if pos is None:
            return None
        fill_price = price * (1 - pos.side * self.slippage)
        gross = pos.side * pos.qty * (fill_price - pos.entry)
        fee = pos.qty * fill_price * self.fee_rate
        self.cash += gross - fee if self.is_perp else pos.qty * fill_price - fee
        self.stats.fees += fee
        net = gross - fee - pos.entry_fee + pos.funding
        self._record_close(pos, fill_price, ts, fee, net, reason)
        self.position = None
        return Fill(ts, "sell" if pos.side == 1 else "buy", pos.qty, fill_price, fee, reason, net,
                    ref_price=price)

    def liquidate(self, ts: int) -> Fill | None:
        pos = self.position
        if pos is None or not self.is_perp:
            return None
        self.cash -= pos.margin
        net = -pos.margin - pos.entry_fee + pos.funding
        self._record_close(pos, pos.liq_price, ts, 0.0, net, "liquidation")
        self.position = None
        return Fill(ts, "sell" if pos.side == 1 else "buy", pos.qty, pos.liq_price, 0.0,
                    "liquidation", net)

    def apply_funding(self, rate: float, price: float, ts: int) -> None:
        pos = self.position
        if pos is None or not self.is_perp or rate == 0:
            return
        payment = pos.side * pos.qty * price * rate  # longs pay shorts when rate > 0
        self.cash -= payment
        pos.funding -= payment
        self.stats.funding += payment

    def state(self) -> dict:
        return {**super().state(), "cash": self.cash}

    def restore(self, state: dict) -> None:
        super().restore(state)
        self.cash = state["cash"]
