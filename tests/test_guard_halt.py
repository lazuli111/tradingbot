"""LIVE_PLAN C8: once the portfolio guard trips, the bots must not open again when the
signal flips. The guard writes data/HALT before flattening; Trader checks it before every
new position (closing still works); --resume removes it. No network."""
import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import portfolio_guard as pg  # noqa: E402
from bot.broker import SimBroker  # noqa: E402
from bot.config import RiskConfig  # noqa: E402
from bot.halt import clear_halt, halt_reason, write_halt  # noqa: E402
from bot.risk import RiskManager  # noqa: E402
from bot.trader import Trader  # noqa: E402
from test_order_sync import FakeExchange, make_runner  # noqa: E402

BAR = 4 * 3600 * 1000


def make_trader(flag: dict) -> Trader:
    broker = SimBroker(True, 10_000, 0.0005, 0.0)
    return Trader(RiskManager(RiskConfig(), True, 10_000), broker, allow_short=True,
                  halt_check=lambda: flag.get("why"))


def bar(trader: Trader, k: int, signal: int, price: float = 100.0) -> None:
    """close of bar k with `signal`, then the open of bar k+1 executes it"""
    trader.on_bar_close(k * BAR, price, signal, atr=2.0)
    trader.on_bar_open((k + 1) * BAR, price)


def test_halt_blocks_new_positions_until_cleared():
    flag = {"why": "组合回撤 -20.10%（阈值 20%）"}
    trader = make_trader(flag)
    bar(trader, 0, 1)
    assert trader.broker.position is None and trader.last_signal == 0
    bar(trader, 1, 1)                                   # still halted: still nothing
    assert trader.broker.position is None
    flag["why"] = None                                  # --resume
    bar(trader, 2, 1)
    assert trader.broker.position.side == 1 and trader.last_signal == 1


def test_halt_still_lets_positions_close_but_not_reverse():
    flag = {}
    trader = make_trader(flag)
    bar(trader, 0, 1)
    assert trader.broker.position.side == 1
    flag["why"] = "人工停机"
    bar(trader, 1, -1)                                  # the long closes, the short is not opened
    assert trader.broker.position is None
    assert [t["reason"] for t in trader.broker.stats.closed_trades] == ["signal"]
    bar(trader, 2, -1)
    assert trader.broker.position is None


def test_halt_reason_reads_the_flag_and_fails_closed(tmp_path):
    path = tmp_path / "data" / "HALT"
    assert halt_reason(path) is None
    write_halt("组合回撤 -20.00%", path)
    assert halt_reason(path) == "组合回撤 -20.00%"
    path.write_text("")
    assert halt_reason(path) == "停机标志存在"
    assert clear_halt(path) is True and clear_halt(path) is False
    path.mkdir()                                        # exists but can't be read as a file
    assert "按已停机处理" in halt_reason(path)


def test_live_runner_checks_the_configured_flag(monkeypatch, tmp_path):
    monkeypatch.setattr("bot.broker.ccxt_broker.time.sleep", lambda s: None)
    monkeypatch.chdir(tmp_path)                         # default halt_file is data/HALT, relative
    runner, _, _ = make_runner(monkeypatch, tmp_path, FakeExchange())
    assert runner.trader.halt_check() is None
    write_halt("组合已停机", tmp_path / "data" / "HALT")
    assert runner.trader.halt_check() == "组合已停机"


class GuardExchange:
    def __init__(self, equity: float, positions: list[dict]):
        self.equity, self.positions = equity, positions
        self.orders: list[dict] = []

    def fetch_balance(self):
        return {"USDT": {"total": self.equity, "free": self.equity}}

    def fetch_positions(self):
        return self.positions

    def amount_to_precision(self, symbol, qty):
        return str(qty)

    def create_order(self, symbol, type_, side, amount, price, params):
        self.orders.append({"symbol": symbol, "side": side, "halt_already": halt_reason(pg.HALT)})


@pytest.fixture
def guard_files(monkeypatch, tmp_path):
    monkeypatch.setattr(pg, "STATE", tmp_path / "portfolio_guard.json")
    monkeypatch.setattr(pg, "HALT", tmp_path / "HALT")
    return tmp_path


def args(**kw):
    return argparse.Namespace(**{"resume": False, "status": False, "flatten": False, "max_dd": 0.20, **kw})


def long_btc():
    return [{"symbol": "BTC/USDT:USDT", "contracts": 0.0035, "side": "long", "entryPrice": 80_000}]


def test_guard_writes_the_flag_before_flattening(guard_files):
    pg.save_state({"peak": 1000.0, "halted": False, "reason": None})
    ex = GuardExchange(790.0, long_btc())
    assert pg.check(ex, args()) == 3
    assert ex.orders == [{"symbol": "BTC/USDT:USDT", "side": "sell", "halt_already": "组合回撤 -21.00%（阈值 20%）"}]
    assert halt_reason(pg.HALT) == "组合回撤 -21.00%（阈值 20%）"
    assert pg.load_state(0)["halted"] is True


def test_guard_restores_a_missing_flag_while_halted(guard_files, capsys):
    pg.save_state({"peak": 1000.0, "halted": True, "reason": "组合回撤 -20.50%（阈值 20%）"})
    ex = GuardExchange(800.0, [])
    assert pg.check(ex, args()) == 0
    assert halt_reason(pg.HALT) == "组合回撤 -20.50%（阈值 20%）"
    assert ex.orders == []
    assert "已补写" in capsys.readouterr().out


def test_resume_removes_the_flag(guard_files, capsys):
    pg.save_state({"peak": 1000.0, "halted": True, "reason": "x"})
    write_halt("x", pg.HALT)
    assert pg.check(GuardExchange(800.0, []), args(resume=True)) == 0
    assert halt_reason(pg.HALT) is None
    assert pg.load_state(0) == {**pg.load_state(0), "peak": 800.0, "halted": False}
    assert "已删除" in capsys.readouterr().out


def test_status_shows_a_manual_flag(guard_files, capsys):
    pg.save_state({"peak": 1000.0, "halted": False, "reason": None})
    write_halt("人工停机：检查对账", pg.HALT)
    assert pg.check(GuardExchange(990.0, []), args(status=True)) == 0
    assert "[停机标志：人工停机：检查对账，机器人不开新仓]" in capsys.readouterr().out
