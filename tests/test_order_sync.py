"""Order outcome / position reconciliation against an in-memory fake exchange (no network).

Background: on 2026-10-22 a reduceOnly close on Binance Demo filled, but fetch_order
answered -2013 "Order does not exist."; the bot kept believing it was short for 6.5 hours.
"""
import math

import ccxt
import numpy as np
import pandas as pd
import pytest

from bot.broker import Position
from bot.broker.ccxt_broker import RECONCILED, CcxtBroker, OrderStateUnknown
from bot.config import Config, RiskConfig
from bot.risk import RiskManager
from bot.trader import Trader
from conftest import make_ohlcv

SYMBOL = "BTC/USDT:USDT"
NOT_FOUND = ccxt.OrderNotFound('binance {"code":-2013,"msg":"Order does not exist."}')


class FakeExchange:
    """Just enough of ccxt's binanceusdm interface. Market orders fill at `last`."""

    def __init__(self, pos: float = 0.0, entry: float = 0.0, last: float = 100.0):
        self.pos, self.entry, self.last = pos, entry, last   # pos: signed contracts
        self.orders: list[dict] = []
        self.trades: dict[str, list] = {}
        self.create_status = "open"        # Binance answers NEW for market orders
        self.record_trades = False         # Demo's trade history can lag too
        self.fetch_order_errors: list[Exception] = []
        self.position_errors: list[Exception] = []
        self.on_create = None
        self.balance = 10_000.0

    # ---- setup / market data
    def load_markets(self):
        return {}

    def market(self, symbol):
        return {"base": "BTC", "quote": "USDT", "contractSize": 1,
                "limits": {"amount": {"min": 0.001}}}

    def set_margin_mode(self, *a):
        pass

    def set_leverage(self, *a):
        pass

    def set_position_mode(self, *a):
        pass

    def fetch_position_mode(self, symbol):
        return {"hedged": False}

    def amount_to_precision(self, symbol, amount):
        return str(math.floor(amount * 1000 + 1e-9) / 1000)

    def fetch_ticker(self, symbol):
        return {"last": self.last}

    def fetch_balance(self):
        return {"USDT": {"total": self.balance}}

    # ---- trading
    def create_order(self, symbol, type_, side, amount, price, params):
        signed = amount if side == "buy" else -amount
        if params.get("reduceOnly") and (self.pos == 0 or self.pos * signed > 0 or abs(signed) > abs(self.pos) + 1e-12):
            raise ccxt.InvalidOrder('binance {"code":-2022,"msg":"ReduceOnly Order is rejected."}')
        oid = str(len(self.orders) + 1)
        self.orders.append({"id": oid, "side": side, "amount": amount, "price": self.last, "params": params})
        if self.pos == 0 or self.pos * signed > 0:
            self.entry = (abs(self.pos) * self.entry + amount * self.last) / (abs(self.pos) + amount)
        self.pos = round(self.pos + signed, 9)
        if self.record_trades:
            self.trades[oid] = [{"amount": amount, "price": self.last,
                                 "fee": {"cost": amount * self.last * 0.0005, "currency": "USDT"}}]
        if self.on_create:
            self.on_create()
        return {"id": oid, "status": self.create_status, "average": None, "filled": 0.0}

    def fetch_order(self, oid, symbol):
        if self.fetch_order_errors:
            raise self.fetch_order_errors.pop(0)
        o = next(o for o in self.orders if o["id"] == oid)
        return {"id": oid, "status": "closed", "filled": o["amount"], "average": o["price"], "fee": None}

    def fetch_my_trades(self, symbol, since, limit, params):
        return self.trades.get(params["orderId"], [])

    def fetch_positions(self, symbols):
        if self.position_errors:
            raise self.position_errors.pop(0)
        if self.pos == 0:
            return []
        return [{"side": "long" if self.pos > 0 else "short", "contracts": abs(self.pos),
                 "entryPrice": self.entry, "timestamp": 0, "initialMargin": 0, "liquidationPrice": None}]


@pytest.fixture
def sleeps(monkeypatch):
    calls = []
    monkeypatch.setattr("bot.broker.ccxt_broker.time.sleep", calls.append)
    return calls


def short_broker(ex: FakeExchange) -> CcxtBroker:
    broker = CcxtBroker(ex, SYMBOL, is_perp=True, leverage=1, testnet=True)
    assert broker.position.side == -1
    broker.position.stop = 120.0
    return broker


def test_testnet_guard_still_there():
    with pytest.raises(NotImplementedError):
        CcxtBroker(FakeExchange(), SYMBOL, is_perp=True, leverage=1, testnet=False)


def test_fetch_order_not_found_then_found(sleeps):
    ex = FakeExchange(pos=-0.01, entry=110)
    ex.fetch_order_errors = [NOT_FOUND, ccxt.RequestTimeout("timeout")]
    broker = short_broker(ex)
    fill = broker.close(100, ts=1, reason="signal")
    assert sleeps == [0.5, 1.0]
    assert fill.qty == pytest.approx(0.01) and fill.price == 100
    assert fill.pnl == pytest.approx(0.01 * 10)
    assert fill.reason == "signal"
    assert broker.position is None and broker.unresolved is None


def test_fetch_order_keeps_failing_but_position_is_gone(sleeps):
    ex = FakeExchange(pos=-0.01, entry=110)
    ex.fetch_order_errors = [NOT_FOUND] * 10
    broker = short_broker(ex)
    fill = broker.close(99, ts=1, reason="signal")      # exchange filled it at 100
    assert sleeps == [0.5, 1.0, 2.0]
    assert broker.position is None and broker.unresolved is None
    assert fill.qty == pytest.approx(0.01)
    # no trade record to read the price from: the decision price stands in, and says so
    assert fill.price == 99 and "估算" in fill.reason
    assert fill.pnl == pytest.approx(0.01 * 11)
    assert broker.stats.closed_trades[-1]["reason"] == fill.reason


def test_fetch_order_failing_uses_trade_record_for_price(sleeps):
    ex = FakeExchange(pos=-0.01, entry=110)
    ex.fetch_order_errors = [NOT_FOUND] * 10
    ex.record_trades = True
    broker = short_broker(ex)
    fill = broker.close(99, ts=1, reason="signal")
    assert fill.price == pytest.approx(100) and fill.reason == "signal"
    assert fill.fee == pytest.approx(0.0005)


def test_open_with_fetch_failing_takes_entry_from_position(sleeps):
    ex = FakeExchange(last=101)
    ex.fetch_order_errors = [NOT_FOUND] * 10
    broker = CcxtBroker(ex, SYMBOL, is_perp=True, leverage=1, testnet=True)
    fill = broker.open(1, 0.02, 100, ts=1, reason="signal")
    assert fill.price == pytest.approx(101) and fill.reason == "signal"
    assert broker.position.side == 1 and broker.position.qty == pytest.approx(0.02)


def test_unknown_outcome_blocks_orders_until_reconciled(sleeps):
    ex = FakeExchange(pos=-0.01, entry=110)
    ex.fetch_order_errors = [NOT_FOUND] * 10
    broker = short_broker(ex)
    ex.on_create = lambda: ex.position_errors.append(ccxt.NetworkError("timeout"))
    with pytest.raises(OrderStateUnknown):
        broker.close(100, ts=1, reason="signal")
    assert broker.unresolved["id"] == "1"
    assert broker.position.side == -1                    # still what we believed
    with pytest.raises(OrderStateUnknown):               # no second order while in doubt
        broker.close(100, ts=2, reason="signal")
    assert len(ex.orders) == 1
    fill = broker.reconcile(100, ts=3)
    assert broker.unresolved is None and broker.position is None
    assert fill.pnl is None and RECONCILED in fill.reason


def test_reduce_only_rejected_but_exchange_flat(sleeps):
    ex = FakeExchange(pos=-0.01, entry=110)
    broker = short_broker(ex)
    ex.pos = 0                                           # closed without the bot knowing
    risk = RiskManager(RiskConfig(), is_perp=True, initial_equity=10_000)
    trader = Trader(risk, broker, allow_short=True)
    trader.last_signal, trader.pending = -1, (1, 2.0)
    trader.execute_pending(ts=1, price=100)
    assert trader.pending is None and trader.last_signal == 1
    assert broker.position.side == 1 and ex.pos > 0      # went on to open the long
    closed = broker.stats.closed_trades[0]
    assert closed["pnl"] is None and RECONCILED in closed["reason"]
    assert [o["side"] for o in ex.orders] == ["buy"]     # one open, the rejected close never counted


def test_reduce_only_rejected_with_position_still_there_raises():
    ex = FakeExchange(pos=-0.01, entry=110)
    broker = short_broker(ex)
    broker.position.qty = 0.02                           # bigger than what the exchange has
    with pytest.raises(ccxt.InvalidOrder):
        broker.close(100, ts=1, reason="signal")
    assert broker.position.qty == pytest.approx(0.01)    # but now we know the real size


def test_reconcile_adopts_exchange_and_trader_sets_a_stop():
    ex = FakeExchange()
    broker = CcxtBroker(ex, SYMBOL, is_perp=True, leverage=1, testnet=True)
    ex.pos, ex.entry = 0.03, 100.0                       # an open we never saw complete
    trader = Trader(RiskManager(RiskConfig(), True, 10_000), broker, allow_short=True)
    trader.last_atr = 2.0
    trader.reconcile(ts=1, price=100)
    assert broker.position.qty == pytest.approx(0.03)
    assert broker.position.stop == pytest.approx(100 - 2.0 * 2.0)


def test_pending_survives_an_exception_and_is_dropped_when_signal_reverts():
    class Boom(SimLike):
        def close(self, *a):
            raise ccxt.NetworkError("down")

    trader = Trader(RiskManager(RiskConfig(), True, 10_000), Boom(), allow_short=True)
    trader.last_signal = -1
    trader.on_bar_close(ts=0, close=100, signal=1, atr=2.0)
    with pytest.raises(ccxt.NetworkError):
        trader.on_bar_open(ts=1, price=100)
    assert trader.pending == (1, 2.0)
    trader.on_bar_close(ts=2, close=100, signal=-1, atr=2.0)
    assert trader.pending is None


class SimLike:
    position = Position(side=-1, qty=1, entry=100, opened_at=0, stop=120)
    unresolved = None

    def equity(self, price):
        return 10_000.0


def test_spot_unknown_order_resolved_later(sleeps):
    ex = FakeExchange()
    ex.market = lambda s: {"base": "BTC", "quote": "USDT", "limits": {"amount": {"min": 0.001}}}
    broker = CcxtBroker(ex, "BTC/USDT", is_perp=False, leverage=1, testnet=True)
    ex.fetch_order_errors = [NOT_FOUND] * 4
    with pytest.raises(OrderStateUnknown):
        broker.open(1, 0.01, 100, ts=1, reason="signal")
    assert broker.position is None and broker.unresolved
    state = broker.state()                               # survives a restart
    broker.restore({"position": None, "unresolved": None})
    broker.restore(state)
    fill = broker.reconcile(100, ts=2)                   # fetch_order works again
    assert broker.unresolved is None
    assert fill.side == "buy" and broker.position.qty == pytest.approx(0.01)
    assert len(ex.orders) == 1


# ---- the whole incident, through the live runner ---------------------------------

class StubStrategy:
    name, warmup = "stub", 1

    def __init__(self):
        self.signal = 1

    def signals(self, df):
        return pd.Series(self.signal, index=df.index)


def make_runner(monkeypatch, tmp_path, ex):
    from bot.engine import live

    bars = make_ohlcv(np.full(60, 100.0), start_ts=0)
    queue: list[pd.DataFrame] = []
    monkeypatch.setattr(live, "make_exchange", lambda *a, **k: ex)
    monkeypatch.setattr(live, "api_credentials", lambda _: {"apiKey": "k", "secret": "s"})
    monkeypatch.setattr(live, "create_strategy", lambda *a: StubStrategy())
    monkeypatch.setattr(live, "fetch_ohlcv_range", lambda *a: queue.pop(0) if queue else bars.iloc[0:0])
    cfg = Config(exchange="binance", market="perp", symbol="BTC/USDT", timeframe="4h")
    cfg.live.db_path = str(tmp_path / "trades.db")
    cfg.live.equity_fraction = 0.5
    runner = live.LiveRunner(cfg, "testnet")
    runner.df = bars
    return runner, queue, bars


def test_incident_replay(monkeypatch, tmp_path, sleeps):
    ex = FakeExchange(pos=-0.01, entry=110)
    runner, queue, bars = make_runner(monkeypatch, tmp_path, ex)
    trader = runner.trader
    trader.last_signal = -1
    trader.broker.position.stop = 120.0

    # 00:00 bar closes with signal +1. The close fills, fetch_order says -2013 and the
    # position can't be read either: the order's outcome is unknown.
    ex.fetch_order_errors = [NOT_FOUND] * 10
    ex.on_create = lambda: ex.position_errors.append(ccxt.NetworkError("timeout"))
    queue.append(make_ohlcv([100.0], start_ts=int(bars["timestamp"].iloc[-1]) + 4 * 3600_000))
    runner.poll()
    assert ex.pos == 0                                   # exchange: flat
    assert trader.broker.position.side == -1             # bot: still short
    assert trader.last_signal == -1

    # next poll (~30 s later, no new bar): reconcile, then redo the bar -> open the long
    ex.on_create = None
    ex.fetch_order_errors = []
    runner.poll()
    assert trader.broker.unresolved is None
    assert trader.broker.position.side == 1 and ex.pos > 0
    assert trader.broker.position.stop is not None
    assert trader.last_signal == 1 and trader.pending is None
    assert [o["side"] for o in ex.orders] == ["buy", "buy"]   # one close, one open, no duplicates
    assert ex.orders[0]["params"] == {"reduceOnly": True} and ex.orders[1]["params"] == {}
    fills = runner.storage.fills()
    assert len(fills) == 2
    assert fills[0][5] is None and RECONCILED in fills[0][6]  # close: PnL marked unknown
    assert fills[1][1] == "buy" and fills[1][6] == "signal"

    runner.poll()                                        # nothing more to do
    assert len(ex.orders) == 2


def test_stale_saved_state_is_reconciled_on_start(monkeypatch, tmp_path, sleeps):
    """The state the server was left in at 04:00: saved short, exchange already flat."""
    ex = FakeExchange(pos=-0.01, entry=110)
    runner, _, _ = make_runner(monkeypatch, tmp_path, ex)
    runner.trader.last_signal = -1
    runner._save()
    ex.pos = 0
    runner2, _, _ = make_runner(monkeypatch, tmp_path, ex)
    assert runner2.trader.broker.position is None
    fills = runner2.storage.fills()
    assert len(fills) == 1 and fills[0][5] is None and RECONCILED in fills[0][6]
    runner2._on_bar_closed()                             # run() does this first; signal +1 -> long
    assert ex.pos > 0 and len(ex.orders) == 1
