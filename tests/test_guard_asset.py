"""The portfolio guard runs once per margin asset (`--asset USDT|USDC`) on one Binance Demo futures
account, so the USDT bots (BTC + SOL) and the USDC basket bot never trip each other: equity is that
asset's balance, positions are the ones margined in it, and state / halt flag are per asset.

Which positions belong to which guard: the settle currency in ccxt's unified symbol, the part after
the colon ("BTC/USDC:USDC" -> USDC). ccxt takes it from exchangeInfo's marginAsset; the bots trade
the same symbols. `info.marginAsset` is not read. No network."""
import argparse
import shlex
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import portfolio_guard as pg  # noqa: E402
from bot.halt import halt_reason, write_halt  # noqa: E402

USDT_FLAG, USDC_FLAG = Path("data/HALT"), Path("data/HALT_USDC")
USDT_STATE, USDC_STATE = Path("data/portfolio_guard.json"), Path("data/portfolio_guard_usdc.json")


def pos(symbol: str, contracts: float, side: str = "long") -> dict:
    """a position the way ccxt's binance fetch_positions returns it"""
    settle = symbol.split(":")[1]
    return {"symbol": symbol, "contracts": contracts, "side": side, "entryPrice": 100.0,
            "info": {"symbol": symbol.split("/")[0] + settle, "marginAsset": settle}}


class AccountExchange:
    """One futures account with USDT and USDC wallets. Like Binance positionRisk, fetch_positions
    returns every position in the account; a reduceOnly market order removes the position."""

    def __init__(self, usdt: float, usdc: float, positions: list[dict]):
        self.balances = {"USDT": usdt, "USDC": usdc}
        self.positions = positions
        self.orders: list[dict] = []

    def load_markets(self):
        return {}

    def fetch_balance(self):
        return {a: {"total": v, "free": v} for a, v in self.balances.items()}

    def fetch_positions(self):
        return [dict(p) for p in self.positions]

    def amount_to_precision(self, symbol, qty):
        return str(qty)

    def create_order(self, symbol, type_, side, amount, price, params):
        assert type_ == "market" and params == {"reduceOnly": True}
        self.orders.append({"symbol": symbol, "side": side, "amount": amount,
                            "flags": (halt_reason(USDT_FLAG), halt_reason(USDC_FLAG))})
        self.positions = [p for p in self.positions if p["symbol"] != symbol]

    def symbols(self) -> list[str]:
        return [p["symbol"] for p in self.positions]


def mixed_book() -> list[dict]:
    return [pos("BTC/USDT:USDT", 0.0035), pos("SOL/USDT:USDT", 2.0, "short"),
            pos("BTC/USDC:USDC", 0.01), pos("ETH/USDC:USDC", 0.5, "short"), pos("XRP/USDC:USDC", 0.0)]


@pytest.fixture(autouse=True)
def in_tmp(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)          # the guard's default paths are relative: data/...
    return tmp_path


def args(**kw):
    return argparse.Namespace(**{"resume": False, "status": False, "flatten": False, "max_dd": 0.20,
                                 "asset": "USDT", **kw})


def test_files_per_asset():
    assert pg.files_for("USDT") == (USDT_STATE, USDT_FLAG)       # unchanged since before --asset
    # the USDC basket bot's config has live.halt_file: data/HALT_USDC -- keep the two in step
    assert pg.files_for("USDC") == (USDC_STATE, USDC_FLAG)


def test_margin_asset_is_the_settle_currency_in_the_symbol():
    assert pg.margin_asset({"symbol": "BTC/USDT:USDT"}) == "USDT"
    assert pg.margin_asset({"symbol": "BTC/USDC:USDC"}) == "USDC"       # no info needed
    assert pg.margin_asset({"symbol": "BTC/USDT:USDT-261225"}) == "USDT"
    assert pg.margin_asset({"symbol": "BTC/USDT", "info": {"marginAsset": "USDT"}}) is None
    ex = AccountExchange(1000, 5000, mixed_book())
    assert [p["symbol"] for p in pg.open_positions(ex)] == ["BTC/USDT:USDT", "SOL/USDT:USDT"]
    assert [p["symbol"] for p in pg.open_positions(ex, "USDC")] == ["BTC/USDC:USDC", "ETH/USDC:USDC"]


def test_usdt_trip_flattens_only_usdt(capsys):
    pg.save_state({"peak": 1000.0, "halted": False, "reason": None}, USDT_STATE)
    ex = AccountExchange(790.0, 5000.0, mixed_book())
    assert pg.check(ex, args()) == 3
    reason = "组合回撤 -21.00%（阈值 20%）"
    assert ex.orders == [
        {"symbol": "BTC/USDT:USDT", "side": "sell", "amount": 0.0035, "flags": (reason, None)},
        {"symbol": "SOL/USDT:USDT", "side": "buy", "amount": 2.0, "flags": (reason, None)},
    ]
    assert ex.symbols() == ["BTC/USDC:USDC", "ETH/USDC:USDC", "XRP/USDC:USDC"]
    assert halt_reason(USDT_FLAG) == reason and not USDC_FLAG.exists()
    assert pg.load_state(0, USDT_STATE)["halted"] is True and not USDC_STATE.exists()
    assert "持仓 2  " in capsys.readouterr().out


def test_usdc_trip_flattens_only_usdc():
    pg.save_state({"peak": 1000.0, "halted": False, "reason": None}, USDT_STATE)
    pg.save_state({"peak": 5000.0, "halted": False, "reason": None}, USDC_STATE)
    ex = AccountExchange(1000.0, 3900.0, mixed_book())
    assert pg.check(ex, args(asset="USDC")) == 3
    reason = "组合回撤 -22.00%（阈值 20%）"
    assert ex.orders == [
        {"symbol": "BTC/USDC:USDC", "side": "sell", "amount": 0.01, "flags": (None, reason)},
        {"symbol": "ETH/USDC:USDC", "side": "buy", "amount": 0.5, "flags": (None, reason)},
    ]
    assert ex.symbols() == ["BTC/USDT:USDT", "SOL/USDT:USDT", "XRP/USDC:USDC"]
    assert halt_reason(USDC_FLAG) == reason and not USDT_FLAG.exists()
    assert pg.load_state(0, USDC_STATE)["halted"] is True
    assert pg.load_state(0, USDT_STATE)["halted"] is False

    assert pg.check(ex, args()) == 0                     # the USDT guard carries on as usual
    assert ex.symbols() == ["BTC/USDT:USDT", "SOL/USDT:USDT", "XRP/USDC:USDC"]
    assert not USDT_FLAG.exists()


def test_peaks_and_halted_state_are_per_asset(capsys):
    ex = AccountExchange(1000.0, 5000.0, [])
    assert pg.check(ex, args()) == 0 and pg.check(ex, args(asset="USDC")) == 0
    ex.balances = {"USDT": 850.0, "USDC": 6000.0}        # a USDC new high doesn't lift the USDT peak
    assert pg.check(ex, args()) == 0 and pg.check(ex, args(asset="USDC")) == 0
    assert pg.load_state(0, USDT_STATE)["peak"] == 1000.0
    assert pg.load_state(0, USDC_STATE)["peak"] == 6000.0

    pg.save_state({"peak": 1000.0, "halted": True, "reason": "x"}, USDT_STATE)
    capsys.readouterr()
    assert pg.check(ex, args(asset="USDC")) == 0          # USDT halted: USDC neither reports it nor writes a flag
    out = capsys.readouterr().out
    assert "已停机" not in out and "停机标志" not in out and not USDC_FLAG.exists()
    ex.balances["USDC"] = 4700.0                          # and still trips on its own drawdown
    ex.positions = [pos("BTC/USDC:USDC", 0.01)]
    assert pg.check(ex, args(asset="USDC")) == 3 and ex.positions == []


def test_resume_clears_only_its_own_flag(capsys):
    for path, flag, peak in ((USDT_STATE, USDT_FLAG, 1000.0), (USDC_STATE, USDC_FLAG, 5000.0)):
        pg.save_state({"peak": peak, "halted": True, "reason": "x"}, path)
        write_halt("x", flag)
    ex = AccountExchange(800.0, 3900.0, [])

    assert pg.check(ex, args(asset="USDC", resume=True)) == 0
    assert "已删除 data/HALT_USDC" in capsys.readouterr().out
    assert not USDC_FLAG.exists() and halt_reason(USDT_FLAG) == "x"
    assert pg.load_state(0, USDC_STATE)["halted"] is False and pg.load_state(0, USDC_STATE)["peak"] == 3900.0
    assert pg.load_state(0, USDT_STATE)["halted"] is True and pg.load_state(0, USDT_STATE)["peak"] == 1000.0

    assert pg.check(ex, args(resume=True)) == 0
    assert not USDT_FLAG.exists() and pg.load_state(0, USDT_STATE)["halted"] is False


def test_status_and_manual_flatten_stay_on_their_asset(capsys):
    ex = AccountExchange(1000.0, 5000.0, mixed_book())
    write_halt("人工停机", USDC_FLAG)
    assert pg.check(ex, args(status=True)) == 0
    out = capsys.readouterr().out
    assert "持仓 2  " in out and "停机标志" not in out
    assert "BTC/USDT:USDT" in out and "USDC:USDC" not in out
    assert pg.check(ex, args(asset="USDC", status=True)) == 0
    out = capsys.readouterr().out
    assert "[停机标志：人工停机，机器人不开新仓]" in out
    assert "ETH/USDC:USDC" in out and "USDT:USDT" not in out
    assert ex.orders == []

    assert pg.check(ex, args(flatten=True)) == 0
    assert ex.symbols() == ["BTC/USDC:USDC", "ETH/USDC:USDC", "XRP/USDC:USDC"]
    assert not USDT_FLAG.exists()                        # --flatten doesn't halt


def unit_argv(name: str) -> list[str]:
    """the guard's arguments as the systemd unit runs them"""
    text = (ROOT / "deploy" / "systemd" / name).read_text(encoding="utf-8")
    cmd = next(line for line in text.splitlines() if line.startswith("ExecStart="))
    argv = shlex.split(cmd.removeprefix("ExecStart="))
    return argv[argv.index("tools/portfolio_guard.py") + 1:]


@pytest.mark.parametrize("unit, state, other", [
    ("tradingbot-guard.service", USDT_STATE, USDC_STATE),
    ("tradingbot-guard-usdc.service", USDC_STATE, USDT_STATE),
])
def test_systemd_units_run_their_own_guard(monkeypatch, unit, state, other):
    ex = AccountExchange(1000.0, 5000.0, mixed_book())
    monkeypatch.setattr(pg, "load_dotenv", lambda: None)
    monkeypatch.setattr(pg, "api_credentials", lambda exchange: {"apiKey": "k", "secret": "s"})
    monkeypatch.setattr(pg, "make_demo_exchange", lambda exchange, creds: ex)
    monkeypatch.setattr(sys, "argv", ["portfolio_guard.py", *unit_argv(unit)])
    assert pg.main() == 0
    assert pg.load_state(0, state)["peak"] == (1000.0 if state == USDT_STATE else 5000.0)
    assert not other.exists() and ex.orders == []


def test_usdc_unit_and_timer():
    argv = unit_argv("tradingbot-guard-usdc.service")
    assert argv == unit_argv("tradingbot-guard.service") + ["--asset", "USDC", "--max-dd", "0.20"]
    service = (ROOT / "deploy/systemd/tradingbot-guard-usdc.service").read_text(encoding="utf-8")
    assert "StandardOutput=append:/var/log/tradingbot-guard-usdc.log" in service
    timer = (ROOT / "deploy/systemd/tradingbot-guard-usdc.timer").read_text(encoding="utf-8")
    usdt_timer = (ROOT / "deploy/systemd/tradingbot-guard.timer").read_text(encoding="utf-8")
    assert "OnBootSec=2min\n" in usdt_timer and "OnBootSec=4min30s\n" in timer   # 2.5 min apart
    assert "OnUnitActiveSec=5min\n" in timer and "OnUnitActiveSec=5min\n" in usdt_timer
