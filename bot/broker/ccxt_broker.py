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
        self.hedged = False
        ex.load_markets()
        self.market = ex.market(symbol)
        self.quote = self.market["quote"]
        self.contract_size = float(self.market.get("contractSize") or 1) if is_perp else 1.0
        if is_perp:
            self._setup_perp()
            self.sync_position()

    def _setup_perp(self) -> None:
        # Binance 的 leverage 只收整数，传 1.0 会被判成 malformed（-1102）
        leverage = int(self.leverage) if float(self.leverage).is_integer() else self.leverage
        for call, args in (("set_margin_mode", ("isolated", self.symbol)),
                           ("set_leverage", (leverage, self.symbol)),
                           # 单向持仓：双向模式下普通市价单会被拒（-4061），reduceOnly 也不可用
                           ("set_position_mode", (False, self.symbol))):
            try:
                getattr(self.ex, call)(*args)
            except (ccxt.BaseError, AttributeError) as e:  # already set, or not supported here
                log.warning("%s 失败（可能已设置）: %s", call, e)
        try:
            self.hedged = bool(self.ex.fetch_position_mode(self.symbol).get("hedged"))
        except (ccxt.BaseError, AttributeError) as e:
            log.warning("读取持仓模式失败，按单向持仓处理: %s", e)
        if self.hedged:
            log.warning("账户仍是双向持仓模式，下单将带 positionSide；建议在交易所页面改成单向持仓")

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

    def _fee_from_trades(self, order_id) -> dict:
        """Binance's futures order payload carries no fee; the trade record does."""
        if order_id is None:
            return {}
        try:
            trades = self.ex.fetch_my_trades(self.symbol, None, None, {"orderId": order_id})
        except (ccxt.BaseError, TypeError) as e:
            log.warning("查成交明细失败，手续费按 0 计: %s", e)
            return {}
        fees = [t.get("fee") or {} for t in trades if (t.get("fee") or {}).get("cost")]
        if not fees:
            return {}
        currency = {f.get("currency") for f in fees}
        return {"currency": currency.pop() if len(currency) == 1 else self.quote,
                "cost": sum(float(f["cost"]) for f in fees)}

    def _send(self, side: str, qty: float, price: float, ts: int, reason: str,
              reduce_only: bool = False, position_side: str | None = None) -> Fill | None:
        amount = self._amount(qty)
        min_amount = (self.market.get("limits", {}).get("amount", {}) or {}).get("min") or 0
        if amount <= 0 or amount < min_amount:
            log.warning("下单数量 %s 低于交易所最小值 %s，跳过", amount, min_amount)
            return None
        params = {}
        if self.is_perp:
            if self.hedged and position_side:
                params["positionSide"] = position_side  # 双向模式：靠 positionSide 区分开平仓
            elif reduce_only:
                params["reduceOnly"] = True
        order = self.ex.create_order(self.symbol, "market", side, amount, None, params)
        if order.get("status") != "closed" or not order.get("average"):
            order = self.ex.fetch_order(order["id"], self.symbol)
        filled = float(order.get("filled") or amount) * self.contract_size
        avg = float(order.get("average") or price)
        fee_info = order.get("fee") or self._fee_from_trades(order.get("id"))
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
        fill = self._send("buy" if side == 1 else "sell", qty, price, ts, reason,
                          position_side="LONG" if side == 1 else "SHORT")
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
                          reduce_only=True, position_side="LONG" if pos.side == 1 else "SHORT")
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
