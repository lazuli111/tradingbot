"""Slippage bookkeeping: every fill keeps the price the bot decided at (and the stop level
on stop exits), the ledger stores it, and forward_report turns it into bp. No network."""
import sqlite3
import sys
from pathlib import Path

import ccxt
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import forward_report as fr  # noqa: E402
from bot.broker import Fill, SimBroker  # noqa: E402
from bot.broker.ccxt_broker import CcxtBroker  # noqa: E402
from bot.config import RiskConfig  # noqa: E402
from bot.risk import RiskManager  # noqa: E402
from bot.storage import Storage  # noqa: E402
from bot.trader import Trader  # noqa: E402
from test_order_sync import NOT_FOUND, SYMBOL, FakeExchange  # noqa: E402

RUN = "testnet:binance:perp:BTC/USDT:USDT:4h:ma_cross"
STEP = 4 * 3600 * 1000


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("bot.broker.ccxt_broker.time.sleep", lambda s: None)


def test_exchange_fill_keeps_decision_price_and_order_id():
    ex = FakeExchange(last=100.5)                      # the market moved after we decided at 100
    broker = CcxtBroker(ex, SYMBOL, is_perp=True, leverage=1, testnet=True)
    fill = broker.open(1, 0.01, price=100.0, ts=1, reason="signal")
    assert fill.price == 100.5 and fill.ref_price == 100.0
    assert fill.order_id == "1"
    assert fr.slippage_bps(fill.side, fill.price, fill.ref_price) == pytest.approx(50)


def test_estimated_fill_price_is_not_counted_as_slippage():
    ex = FakeExchange(pos=-0.01, entry=110, last=100)
    ex.fetch_order_errors = [NOT_FOUND] * 10           # never found, no trade record either
    broker = CcxtBroker(ex, SYMBOL, is_perp=True, leverage=1, testnet=True)
    fill = broker.close(100, ts=1, reason="signal")
    assert "估算" in fill.reason and fill.ref_price is None


def test_soft_stop_exit_records_the_stop_level():
    broker = SimBroker(True, 10_000, 0.0005, 0.0005, 1, 0.004)
    trader = Trader(RiskManager(RiskConfig(), True, 10_000), broker, allow_short=True)
    seen = []
    trader.on_fill = seen.append
    broker.open(1, 1.0, 100.0, 0, "signal")
    broker.position.stop = 95.0
    trader.check_exits(1, 94.0, 94.0, 94.0)            # gapped through the stop
    (fill,) = seen
    assert fill.reason == "stop_loss" and fill.trigger_price == 95.0 and fill.ref_price == 94.0
    assert fr.slippage_bps(fill.side, fill.price, fill.trigger_price) > 100


def old_ledger(path: Path) -> None:
    """A ledger as written before the slippage columns existed."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE fills (id INTEGER PRIMARY KEY AUTOINCREMENT, run TEXT NOT NULL, ts INTEGER NOT NULL,
            side TEXT NOT NULL, qty REAL NOT NULL, price REAL NOT NULL, fee REAL NOT NULL, pnl REAL,
            reason TEXT NOT NULL);
        CREATE TABLE equity (run TEXT NOT NULL, ts INTEGER NOT NULL, equity REAL NOT NULL, PRIMARY KEY (run, ts));
    """)
    conn.execute("INSERT INTO fills (run, ts, side, qty, price, fee, pnl, reason) VALUES (?,?,?,?,?,?,?,?)",
                 (RUN, 1, "buy", 0.0035, 84630.3, 0.118, None, "signal"))
    conn.commit()
    conn.close()


def test_report_reads_a_ledger_without_the_new_columns(tmp_path):
    db = tmp_path / "portfolio.db"
    old_ledger(db)
    _, fills = fr.load_ledger(str(db), RUN)
    assert fills[0]["price"] == 84630.3 and fills[0]["ref_price"] is None


def test_storage_adds_the_columns_to_an_old_ledger(tmp_path):
    db = tmp_path / "portfolio.db"
    old_ledger(db)
    Storage(db, RUN)                                    # the second process finds them there
    storage = Storage(db, RUN)
    storage.record_fill(Fill(2, "sell", 0.0035, 83222.8, 0.116, "stop_loss", -5.16,
                             ref_price=83230.0, trigger_price=83250.0, order_id="42"))
    _, fills = fr.load_ledger(str(db), RUN)
    assert [f["ref_price"] for f in fills] == [None, 83230.0]
    assert fills[1]["trigger_price"] == 83250.0 and fills[1]["order_id"] == "42"


def fill(ts, side, price, reason="signal", ref=None, trigger=None):
    return {"ts": ts, "side": side, "qty": 1.0, "price": price, "fee": 0.05, "pnl": None,
            "reason": reason, "ref_price": ref, "trigger_price": trigger, "order_id": None}


def test_slippage_report_numbers_and_checks():
    bar = 10 * STEP
    fills = [
        fill(bar + 5_000, "buy", 100.05, ref=100.0),              # +5 bp
        fill(bar + STEP + 5_000, "sell", 99.90, ref=100.0),       # +10 bp
        fill(bar + STEP + 9_000, "sell", 94.91, "stop_loss", ref=94.95, trigger=95.0),    # +9.5 bp
        fill(bar + 2 * STEP, "buy", 120.0, "signal（成交价估算/已与交易所核对）", ref=100.0),  # skipped
        fill(1, "buy", 100.0),                                     # before slippage was recorded
    ]
    lines = fr.slippage_report(fills, {bar: 100.0, bar + STEP: 100.0}, STEP)
    text = "\n".join(lines)
    assert "1 笔没有参考价" in text
    assert "市价单 vs 决策价     2 笔  平均 +7.5" in text
    assert "市价单 vs K 线开盘价 2 笔  平均 +7.5" in text
    assert "止损/止盈 vs 触发价  1 笔  平均 +9.5" in text
    assert "✗ 市价单平均滑点 +7.5 ≤ 5 bp" in text
    assert "✓ 市价单单笔滑点 +10.0 ≤ 20 bp" in text
    assert "✓ 止损单平均滑点 +9.5 ≤ 10 bp" in text


def test_slippage_report_without_usable_fills():
    assert fr.slippage_report([fill(1, "buy", 100.0)], {}, STEP)[-1] == "还没有可以算滑点的成交"
