"""Exchange broker via ccxt. Version 1 only allows testnet / demo-trading accounts."""
from __future__ import annotations

import logging
import math
import time

import ccxt

from .base import Broker, BrokerStats, Fill, Position

log = logging.getLogger(__name__)

FETCH_RETRY_DELAYS = (0.5, 1.0, 2.0)   # seconds to wait before each fetch_order retry
FINAL_STATUSES = ("closed", "canceled", "expired", "rejected")
RECONCILED = "盈亏未知/已与交易所核对"


class OrderStateUnknown(RuntimeError):
    """An order reached the exchange, but whether it filled can't be told yet."""


def _is_reduce_only_rejected(e: Exception) -> bool:
    return "-2022" in str(e) or "ReduceOnly Order is rejected" in str(e)


def _describe(pos: Position | None) -> str:
    return f"{pos.side:+d} {pos.qty:.6f}" if pos else "无持仓"


class CcxtBroker(Broker):
    """Sends market orders to an exchange.

    Stops / take-profits are *soft*: the live runner checks them against the last
    price on every poll and closes with a market order. Perp liquidation and
    funding are handled by the exchange itself.

    spot: the bot tracks its own position locally (testnet accounts come pre-funded
    with many coins, so the balance can't tell us what the bot bought).
    perp: the position is read back from the exchange on start, and `reconcile()`
    makes the exchange the source of truth whenever an order's outcome is in doubt.
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
        self.unresolved: dict | None = None
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

    def _exchange_position(self) -> dict | None:
        positions = [p for p in self.ex.fetch_positions([self.symbol]) if float(p.get("contracts") or 0)]
        return positions[0] if positions else None

    def _signed_qty(self, p: dict | None) -> float:
        """Exchange position as a signed base-asset quantity (long > 0)."""
        if p is None:
            return 0.0
        return (1 if p["side"] == "long" else -1) * float(p["contracts"]) * self.contract_size

    def sync_position(self) -> None:
        p = self._exchange_position()
        if p is None:
            self.position = None
            return
        side = 1 if p["side"] == "long" else -1
        qty = float(p["contracts"]) * self.contract_size
        stop = tp = None
        entry_fee = funding = 0.0
        if self.position and self.position.side == side:
            # keep locally tracked soft stops, and the entry fee / funding we already booked:
            # the exchange's position payload carries neither, and dropping entry_fee would
            # overstate the round-trip PnL by one side's commission.
            stop, tp = self.position.stop, self.position.take_profit
            entry_fee, funding = self.position.entry_fee, self.position.funding
        self.position = Position(side=side, qty=qty, entry=float(p["entryPrice"]),
                                 opened_at=int(p.get("timestamp") or 0),
                                 entry_fee=entry_fee, funding=funding,
                                 margin=float(p.get("initialMargin") or 0),
                                 liq_price=float(p["liquidationPrice"]) if p.get("liquidationPrice") else None,
                                 stop=stop, take_profit=tp)

    def reconcile(self, price: float, ts: int) -> Fill | None:
        """perp: adopt the exchange position, warning when it differs from ours.
        spot: the exchange can't tell us what the bot holds (pre-funded testnet balances),
        so the most we can do is settle an order whose outcome is still unknown."""
        if not self.is_perp:
            return self._resolve_spot_order()
        before = self.position
        self.sync_position()
        if self.unresolved:
            log.warning("状态未知的订单 %s 已按交易所持仓核对", self.unresolved["id"])
            self.unresolved = None
        after = self.position
        if before is None and after is None:
            return None
        if before and after and before.side == after.side and math.isclose(before.qty, after.qty, rel_tol=1e-6):
            return None
        log.warning("本地持仓与交易所不一致，以交易所为准：本地 %s，交易所 %s", _describe(before), _describe(after))
        if before is not None and (after is None or after.side != before.side):
            return self._closed_elsewhere(before, price, ts, "reconcile")
        return None

    def _resolve_spot_order(self) -> Fill | None:
        u = self.unresolved
        if u is None:
            return None
        try:
            fill = self._resolve(u["id"], u["side"], u["amount"], u["price"], u["ts"], u["reason"], 0.0)
        except OrderStateUnknown:
            return None  # still unknown: _resolve logged it, orders stay blocked
        self.unresolved = None
        if fill is None:
            log.warning("状态未知的订单 %s 核对结果：未成交", u["id"])
            return None
        log.warning("状态未知的订单 %s 核对结果：已成交 %.6f @ %.2f", u["id"], fill.qty, fill.price)
        if self.position is None:
            self._apply_open(1 if fill.side == "buy" else -1, fill, u["ts"])
            return fill
        return self._apply_close(self.position, fill, u["ts"])

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

    def _order_trades(self, order_id) -> list[dict] | None:
        """Our trades for one order; None if they couldn't be fetched."""
        if order_id is None:
            return None
        try:
            return self.ex.fetch_my_trades(self.symbol, None, None, {"orderId": order_id})
        except (ccxt.BaseError, TypeError) as e:
            log.warning("查成交明细失败: %s", e)
            return None

    def _fee_from_trades(self, trades: list[dict] | None) -> dict:
        """Binance's futures order payload carries no fee; the trade record does."""
        fees = [t.get("fee") or {} for t in trades or [] if (t.get("fee") or {}).get("cost")]
        if not fees:
            return {}
        currency = {f.get("currency") for f in fees}
        return {"currency": currency.pop() if len(currency) == 1 else self.quote,
                "cost": sum(float(f["cost"]) for f in fees)}

    def _fetch_order(self, order_id) -> dict | None:
        """fetch_order with backoff. Right after a fill Binance Demo has answered -2013
        "Order does not exist." for an order that did fill (2026-10-22). Returns the
        last order seen (possibly not final yet), or None if it was never found."""
        order = None
        for attempt, delay in enumerate((0.0, *FETCH_RETRY_DELAYS), 1):
            if delay:
                time.sleep(delay)
            try:
                order = self.ex.fetch_order(order_id, self.symbol)
            except (ccxt.OrderNotFound, ccxt.NetworkError) as e:
                log.warning("查询订单 %s 失败（第 %d 次）: %s", order_id, attempt, e)
                continue
            if order.get("status") in FINAL_STATUSES:
                return order
        return order

    def _make_fill(self, side: str, filled: float, avg: float, fee_info: dict, ts: int,
                   reason: str, ref_price: float | None, order_id) -> Fill:
        fee = self._fee_in_quote(fee_info, avg)
        if not self.is_perp and side == "buy" and fee_info.get("currency") == self.market["base"]:
            filled -= float(fee_info.get("cost") or 0)  # fee was taken out of the coins we bought
        self.stats.fees += fee
        log.info("订单成交 %s %s %.6f @ %.2f (%s)", side, self.symbol, filled, avg, reason)
        return Fill(ts, side, filled, avg, fee, reason, ref_price=ref_price,
                    order_id=None if order_id is None else str(order_id))

    def _fill_from_order(self, order: dict, side: str, amount: float, price: float, ts: int,
                         reason: str) -> Fill | None:
        status = order.get("status")
        filled = float(order.get("filled") or (amount if status == "closed" else 0)) * self.contract_size
        if filled <= 0:
            log.warning("订单 %s 未成交（状态 %s）", order.get("id"), status)
            return None
        avg = float(order.get("average") or price)
        fee_info = order.get("fee") or self._fee_from_trades(self._order_trades(order.get("id")))
        # no average price: avg is our own request price, so there's no slippage to measure
        return self._make_fill(side, filled, avg, fee_info, ts, reason,
                               price if order.get("average") else None, order.get("id"))

    def _resolve(self, order_id, side: str, amount: float, price: float, ts: int, reason: str,
                 before: float) -> Fill | None:
        """Find out what became of an order we sent. `before` is our signed position
        (base asset) before sending it. Returns None if it didn't fill; raises
        OrderStateUnknown (and sets `unresolved`) if that can't be determined."""
        order = self._fetch_order(order_id)
        if order is not None and order.get("status") in FINAL_STATUSES:
            return self._fill_from_order(order, side, amount, price, ts, reason)

        # fetch_order gave up: our trades say at what price, the exchange position says how much
        trades = self._order_trades(order_id) or []
        traded = sum(float(t.get("amount") or 0) for t in trades)
        filled = traded * self.contract_size
        avg = sum(float(t["amount"]) * float(t["price"]) for t in trades) / traded if traded else None
        estimated = False
        if self.is_perp:
            try:
                p = self._exchange_position()
            except ccxt.BaseError as e:
                log.warning("读取交易所持仓失败: %s", e)
            else:
                moved = (1 if side == "buy" else -1) * (self._signed_qty(p) - before)
                filled = min(max(moved, 0.0), amount * self.contract_size)
                if filled > 0 and avg is None:
                    if before == 0 and p is not None:
                        avg = float(p["entryPrice"])  # opened from flat: the entry is our fill price
                    else:
                        avg, estimated = price, True
        if filled <= 1e-12 or avg is None:
            self.unresolved = {"id": order_id, "side": side, "amount": amount, "price": price,
                               "ts": ts, "reason": reason}
            log.error("订单 %s（%s %s %s）状态未知，需要核对：查不到订单，持仓 / 成交记录也判断不出结果",
                      order_id, side, amount, self.symbol)
            raise OrderStateUnknown(f"订单 {order_id} 状态未知，需要核对")
        log.warning("订单 %s 查询失败，按交易所%s判断为已成交 %.6f @ %.2f%s", order_id,
                    "持仓" if self.is_perp else "成交记录", filled, avg, "（成交价为估算）" if estimated else "")
        return self._make_fill(side, filled, avg, self._fee_from_trades(trades), ts,
                               f"{reason}（成交价估算/已与交易所核对）" if estimated else reason,
                               None if estimated else price, order_id)

    def _send(self, side: str, qty: float, price: float, ts: int, reason: str,
              reduce_only: bool = False, position_side: str | None = None) -> Fill | None:
        if self.unresolved:
            raise OrderStateUnknown(f"订单 {self.unresolved['id']} 状态未知，核对之前不再下单")
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
        pos = self.position
        before = pos.side * pos.qty if pos else 0.0
        order = self.ex.create_order(self.symbol, "market", side, amount, None, params)
        if order.get("status") == "closed" and order.get("average"):
            return self._fill_from_order(order, side, amount, price, ts, reason)
        return self._resolve(order["id"], side, amount, price, ts, reason, before)

    def _apply_open(self, side: int, fill: Fill, ts: int) -> None:
        self.position = Position(side=side, qty=fill.qty, entry=fill.price, opened_at=ts,
                                 entry_fee=fill.fee)
        if self.is_perp:
            self.sync_position()

    def _apply_close(self, pos: Position, fill: Fill, ts: int) -> Fill:
        net = pos.side * fill.qty * (fill.price - pos.entry) - fill.fee - pos.entry_fee
        fill.pnl = net
        self._record_close(pos, fill.price, ts, fill.fee, net, fill.reason)
        self.position = None
        return fill

    def _closed_elsewhere(self, pos: Position, price: float, ts: int, reason: str) -> Fill:
        """The exchange no longer has `pos`. Book it as closed with unknown PnL: we don't
        know at what price, `price` is just where the market is now."""
        note = f"{reason}（{RECONCILED}）"
        log.warning("交易所已无 %s 持仓，按已平仓记录，盈亏未知：请到交易所成交记录里核对", _describe(pos))
        self._record_close(pos, price, ts, 0.0, None, note)
        self.position = None
        return Fill(ts, "sell" if pos.side == 1 else "buy", pos.qty, price, 0.0, note)

    def open(self, side: int, qty: float, price: float, ts: int, reason: str) -> Fill | None:
        if self.position is not None:
            raise RuntimeError("已有持仓，请先平仓")
        if side == -1 and not self.is_perp:
            raise ValueError("现货不能做空")
        fill = self._send("buy" if side == 1 else "sell", qty, price, ts, reason,
                          position_side="LONG" if side == 1 else "SHORT")
        if fill is None:
            return None
        self._apply_open(side, fill, ts)
        return fill

    def close(self, price: float, ts: int, reason: str) -> Fill | None:
        pos = self.position
        if pos is None:
            return None
        try:
            fill = self._send("sell" if pos.side == 1 else "buy", pos.qty, price, ts, reason,
                              reduce_only=True, position_side="LONG" if pos.side == 1 else "SHORT")
        except ccxt.InvalidOrder as e:
            if not (self.is_perp and _is_reduce_only_rejected(e)):
                raise
            # -2022: nothing left to reduce. Did an earlier close fill without us knowing?
            log.warning("平仓单被拒（ReduceOnly）: %s；核对交易所持仓", e)
            self.sync_position()
            if self.position is not None:
                raise
            return self._closed_elsewhere(pos, price, ts, reason)
        if fill is None:
            return None
        return self._apply_close(pos, fill, ts)

    def liquidate(self, ts: int) -> Fill | None:
        # The exchange liquidates by itself; just resync our view.
        self.sync_position()
        return None

    def state(self) -> dict:
        return {**super().state(), "unresolved": self.unresolved}

    def restore(self, state: dict) -> None:
        super().restore(state)
        self.unresolved = state.get("unresolved")
