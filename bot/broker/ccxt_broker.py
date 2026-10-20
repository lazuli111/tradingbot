"""Exchange broker via ccxt. Version 1 only allows testnet / demo-trading accounts."""
from __future__ import annotations

import logging

import ccxt

from .base import Broker, BrokerStats, Fill, Position

log = logging.getLogger(__name__)


class CcxtBroker(Broker):
    """Sends market orders to an exchange.

    Stops / take-profits are *soft*: the live runner checks them against the last
    price on every poll and closes with a market order. Perp liquidation and
    funding are handled by the exchange itself.

    spot: the bot tracks its own position locally (testnet accounts come pre-funded
    with many coins, so the balance can't tell us what the bot bought).
    perp: the position is read back from the exchange on start.
    """

    def __init__(self, ex: ccxt.Exchange, symbol: str, is_perp: bool, leverage: float,
                 testnet: bool):
        if not testnet:
            raise NotImplementedError("第一版不支持实盘交易，只能连接测试网 / 模拟盘")
        self.ex = ex
        self.symbol = symbol
        self.is_perp = is_perp
        self.leverage = leverage
        self.position: Position | None = None
        self.stats = BrokerStats()
        ex.load_markets()
        self.market = ex.market(symbol)
        self.quote = self.market["quote"]
        self.contract_size = float(self.market.get("contractSize") or 1) if is_perp else 1.0
        if is_perp:
            self._setup_perp()
            self.sync_position()

    def _setup_perp(self) -> None:
        for call, args in (("set_margin_mode", ("isolated", self.symbol)),
                           ("set_leverage", (self.leverage, self.symbol))):
            try:
                getattr(self.ex, call)(*args)
            except ccxt.BaseError as e:  # already set, or not supported by this exchange
                log.warning("%s 失败（可能已设置）: %s", call, e)

    def sync_position(self) -> None:
        positions = [p for p in self.ex.fetch_positions([self.symbol]) if float(p.get("contracts") or 0)]
        if not positions:
            self.position = None
            return
        p = positions[0]
        side = 1 if p["side"] == "long" else -1
        qty = float(p["contracts"]) * self.contract_size
        stop = tp = None
        if self.position and self.position.side == side:  # keep locally tracked soft stops
            stop, tp = self.position.stop, self.position.take_profit
        self.position = Position(side=side, qty=qty, entry=float(p["entryPrice"]),
                                 opened_at=int(p.get("timestamp") or 0),
                                 margin=float(p.get("initialMargin") or 0),
                                 liq_price=float(p["liquidationPrice"]) if p.get("liquidationPrice") else None,
                                 stop=stop, take_profit=tp)

    def equity(self, price: float) -> float:
        balance = self.ex.fetch_balance()
        quote_total = float(balance.get(self.quote, {}).get("total") or 0)
        pos = self.position
        if pos is None:
            return quote_total
        if self.is_perp:
            return quote_total + pos.side * pos.qty * (price - pos.entry)
        return quote_total + pos.qty * price

    def _amount(self, qty: float) -> float:
        return float(self.ex.amount_to_precision(self.symbol, qty / self.contract_size))

    def _fee_in_quote(self, fee: dict, price: float) -> float:
        cost = float(fee.get("cost") or 0)
        if fee.get("currency") == self.market["base"]:
            return cost * price
        if fee.get("currency") in (None, self.quote):
            return cost
        log.warning("手续费以 %s 计价，未计入统计", fee.get("currency"))  # e.g. paid in BNB
        return 0.0

    def _send(self, side: str, qty: float, price: float, ts: int, reason: str,
              reduce_only: bool = False) -> Fill | None:
        amount = self._amount(qty)
        min_amount = (self.market.get("limits", {}).get("amount", {}) or {}).get("min") or 0
        if amount <= 0 or amount < min_amount:
            log.warning("下单数量 %s 低于交易所最小值 %s，跳过", amount, min_amount)
            return None
        params = {"reduceOnly": True} if reduce_only and self.is_perp else {}
        order = self.ex.create_order(self.symbol, "market", side, amount, None, params)
        if order.get("status") != "closed" or not order.get("average"):
            order = self.ex.fetch_order(order["id"], self.symbol)
        filled = float(order.get("filled") or amount) * self.contract_size
        avg = float(order.get("average") or price)
        fee_info = order.get("fee") or {}
        fee = self._fee_in_quote(fee_info, avg)
        if not self.is_perp and side == "buy" and fee_info.get("currency") == self.market["base"]:
            filled -= float(fee_info.get("cost") or 0)  # fee was taken out of the coins we bought
        self.stats.fees += fee
        log.info("订单成交 %s %s %.6f @ %.2f (%s)", side, self.symbol, filled, avg, reason)
        return Fill(ts, side, filled, avg, fee, reason)

    def open(self, side: int, qty: float, price: float, ts: int, reason: str) -> Fill | None:
        if self.position is not None:
            raise RuntimeError("已有持仓，请先平仓")
        if side == -1 and not self.is_perp:
            raise ValueError("现货不能做空")
        fill = self._send("buy" if side == 1 else "sell", qty, price, ts, reason)
        if fill is None:
            return None
        self.position = Position(side=side, qty=fill.qty, entry=fill.price, opened_at=ts,
                                 entry_fee=fill.fee)
        if self.is_perp:
            self.sync_position()
        return fill

    def close(self, price: float, ts: int, reason: str) -> Fill | None:
        pos = self.position
        if pos is None:
            return None
        fill = self._send("sell" if pos.side == 1 else "buy", pos.qty, price, ts, reason,
                          reduce_only=True)
        if fill is None:
            return None
        net = pos.side * fill.qty * (fill.price - pos.entry) - fill.fee - pos.entry_fee
        fill.pnl = net
        self._record_close(pos, fill.price, ts, fill.fee, net, reason)
        self.position = None
        return fill

    def liquidate(self, ts: int) -> Fill | None:
        # The exchange liquidates by itself; just resync our view.
        self.sync_position()
        return None
