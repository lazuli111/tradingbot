"""tradingbot-usdt.service: the USDT bots' BTC + SOL move from two single-coin processes
(tradingbot-btc / -sol, `main.py testnet --symbol X`) into one `main.py testnet-multi` process.
Strategy and parameters don't change, and neither do the run names, the ledger
(data/portfolio.db) or the halt flag (data/HALT): the state the single-coin processes left
behind is picked up as it is, and the other way round for a rollback.

Each unit's ExecStart goes through main.main as it would on the server, against a copy of
config.portfolio.example.yaml (the server's config.portfolio.yaml is one) and an in-memory
fake exchange (no network)."""
import json
import logging
import shlex
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import main
from bot.config import load_config
from bot.engine import live
from bot.engine.live_multi import MultiRunner
from bot.strategies import create_strategy
from test_live_multi import BasketExchange, Chart
from test_order_sync import StubStrategy

ROOT = Path(__file__).resolve().parents[1]
UNITS = ROOT / "deploy" / "systemd"
CONFIG = ROOT / "config.portfolio.example.yaml"
PRICES = {"BTC/USDT:USDT": 70_000.0, "SOL/USDT:USDT": 150.0}
BTC_RUN = "testnet:binance:perp:BTC/USDT:USDT:4h:ma_cross"
SOL_RUN = "testnet:binance:perp:SOL/USDT:USDT:4h:ma_cross"


class Stub(StubStrategy):
    name = "ma_cross"       # the run name carries the strategy's name: keep the real one


def exec_args(unit: str) -> list[str]:
    """The unit's ExecStart, from `main.py` on."""
    line = next(x for x in (UNITS / unit).read_text(encoding="utf-8").splitlines() if x.startswith("ExecStart="))
    argv = shlex.split(line.removeprefix("ExecStart="))
    return argv[argv.index("main.py") + 1:]


@pytest.fixture
def usdt(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)                 # db_path / halt_file in the config are relative
    shutil.copy(CONFIG, tmp_path / "config.portfolio.yaml")
    monkeypatch.setattr("bot.broker.ccxt_broker.time.sleep", lambda s: None)
    env = SimpleNamespace(ex=BasketExchange(prices=PRICES), chart=Chart(PRICES), tmp=tmp_path)
    monkeypatch.setattr(live, "make_exchange", lambda *a, **k: env.ex)
    monkeypatch.setattr(live, "api_credentials", lambda _: {"apiKey": "k", "secret": "s"})
    monkeypatch.setattr(live, "create_strategy", lambda *a: Stub())
    monkeypatch.setattr(live, "fetch_ohlcv_range", lambda *a: env.chart(*a))

    started = []                                # main.main builds the runner; the loop doesn't start
    monkeypatch.setattr(live.LiveRunner, "run", lambda self, max_bars=None: started.append(self))
    monkeypatch.setattr(MultiRunner, "run", lambda self, max_bars=None: started.append(self))

    def launch(unit: str):
        assert main.main(exec_args(unit)) == 0
        return started.pop()

    env.launch = launch
    return env


def open_with_single_coin_services(usdt, btc_signal=1, sol_signal=-1):
    """What the server has before the switch: BTC and SOL each traded by its own process."""
    btc, sol = usdt.launch("tradingbot-btc.service"), usdt.launch("tradingbot-sol.service")
    btc.strategy.signal, sol.strategy.signal = btc_signal, sol_signal
    btc.start()
    sol.start()
    return btc, sol


def orders(ex):
    return [(o["symbol"], o["side"], o["amount"], o["params"]) for o in ex.orders]


# ---- the unit ----------------------------------------------------------------------

def test_unit_runs_btc_and_sol_in_one_process():
    assert exec_args("tradingbot-usdt.service") == [
        "testnet-multi", "--config", "config.portfolio.yaml", "--symbols", "BTC/USDT,SOL/USDT"]
    unit = (UNITS / "tradingbot-usdt.service").read_text(encoding="utf-8")
    btc = (UNITS / "tradingbot-btc.service").read_text(encoding="utf-8")
    settings = [x for x in unit.splitlines() if x and not x.startswith("#")]
    assert "MemoryHigh=600M" in settings and "MemoryHigh=600M" in btc
    assert not any(x.startswith("MemoryMax=") for x in settings)    # HANDOVER "踩过的坑" 2
    # the one to protect: no positive OOMScoreAdjust (the USDC basket's +500 goes first)
    assert all(int(x.split("=")[1]) <= 0 for x in settings if x.startswith("OOMScoreAdjust="))
    assert settings.count("StandardOutput=append:/var/log/tradingbot-usdt.log") == 1
    assert settings.count("StandardError=append:/var/log/tradingbot-usdt.log") == 1
    for line in ("User=tradingbot", "WorkingDirectory=/opt/tradingbot", "Restart=always", "RestartSec=30",
                 "OPENBLAS_NUM_THREADS=1", "WantedBy=multi-user.target"):
        assert line in unit and line in btc
    for old in ("tradingbot-btc.service", "tradingbot-sol.service"):
        assert "已被 tradingbot-usdt.service 取代" in (UNITS / old).read_text(encoding="utf-8")


def test_same_runs_ledger_halt_flag_and_settings_as_the_single_coin_services(usdt):
    btc, sol = usdt.launch("tradingbot-btc.service"), usdt.launch("tradingbot-sol.service")
    multi = usdt.launch("tradingbot-usdt.service")
    assert list(multi.runners) == ["BTC/USDT", "SOL/USDT"]
    assert (btc.storage.run, sol.storage.run) == (BTC_RUN, SOL_RUN)
    assert create_strategy(btc.cfg.strategy.name, btc.cfg.strategy.params).name == "ma_cross"
    for old in (btc, sol):
        new = multi.runners[old.cfg.symbol]
        assert new.storage.run == old.storage.run
        assert replace(new.cfg, symbols=[]) == old.cfg      # strategy, risk, 4h, ledger, halt flag, 0.5
        assert new.trader.halt_check.args == old.trader.halt_check.args == ("data/HALT",)
        ledger = lambda r: Path(r.storage.conn.execute("PRAGMA database_list").fetchone()[2])
        assert ledger(new) == ledger(old) == usdt.tmp / "data" / "portfolio.db"
        assert new.trader.broker.fraction == old.trader.broker.fraction == 0.5


# ---- picking up where the single-coin processes left off ----------------------------------

def test_multi_runner_resumes_the_single_coin_positions_and_stops(usdt, caplog):
    ex = usdt.ex
    btc, sol = open_with_single_coin_services(usdt)
    assert ex.pos["BTC/USDT:USDT"] > 0 > ex.pos["SOL/USDT:USDT"]
    saved = {r.cfg.symbol: r.storage.load_state()["trader"] for r in (btc, sol)}
    sol_stop = saved["SOL/USDT"]["broker"]["position"]["stop"]
    assert saved["BTC/USDT"]["last_signal"] == 1 and saved["SOL/USDT"]["last_signal"] == -1

    # stop + disable tradingbot-btc / -sol, enable --now tradingbot-usdt
    with caplog.at_level(logging.INFO):
        multi = usdt.launch("tradingbot-usdt.service")
    assert "已恢复上次运行状态 (%s)" in [r.msg for r in caplog.records]
    for symbol, runner in multi.runners.items():
        assert runner.trader.state() == saved[symbol]        # position, stop, last_signal, risk: all of it
    multi.runners["SOL/USDT"].strategy.signal = -1

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        multi._round()                                       # re-acts on the latest bar: same signals
    assert len(ex.orders) == 2                               # nothing sent twice
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]   # no mismatch to reconcile
    for symbol, runner in multi.runners.items():
        assert runner.storage.load_state()["trader"] == saved[symbol]

    # the restored soft stop is the one in force: SOL rallies through it
    ex.last["SOL/USDT:USDT"] = sol_stop * 1.01
    multi._round()
    assert ex.pos["SOL/USDT:USDT"] == 0 and multi.runners["SOL/USDT"].trader.broker.position is None
    [*_, stop_fill] = multi.runners["SOL/USDT"].storage.fills()
    assert stop_fill[6] == "stop_loss" and stop_fill[5] < 0  # P&L off the entry the old process recorded

    # the next bar flips BTC: the close and the new short land in the same run as the old entry
    multi.runners["BTC/USDT"].strategy.signal = -1
    usdt.chart.close_bar(ex)
    assert multi._round() is True
    assert ex.pos["BTC/USDT:USDT"] < 0
    fills = multi.runners["BTC/USDT"].storage.fills()
    assert [f[1] for f in fills] == ["buy", "sell", "sell"]
    assert fills[1][5] is not None                           # the close knew the entry
    assert [o[3] for o in orders(ex)[3:]] == [{"reduceOnly": True}, {}]


def test_rollback_single_coin_services_resume_the_multi_runner_state(usdt):
    ex = usdt.ex
    open_with_single_coin_services(usdt)
    multi = usdt.launch("tradingbot-usdt.service")
    multi.runners["SOL/USDT"].strategy.signal = 1            # SOL flips to long under the multi runner
    usdt.chart.close_bar(ex)
    multi._round()
    multi._save_all()
    assert ex.pos["SOL/USDT:USDT"] > 0 and len(ex.orders) == 4
    saved = {s: r.storage.load_state()["trader"] for s, r in multi.runners.items()}

    # stop + disable tradingbot-usdt, enable --now tradingbot-btc / -sol
    btc, sol = usdt.launch("tradingbot-btc.service"), usdt.launch("tradingbot-sol.service")
    for runner in (btc, sol):
        assert runner.trader.state() == saved[runner.cfg.symbol]
        runner.start()                                       # same signals (Stub: +1): nothing to do
    assert len(ex.orders) == 4
    assert [f[1] for f in sol.storage.fills()] == ["sell", "buy", "buy"]


def test_fresh_start_sends_the_same_orders_as_two_single_coin_processes(usdt, monkeypatch, tmp_path):
    open_with_single_coin_services(usdt)
    before = orders(usdt.ex)
    stops = {r: s["trader"]["broker"]["position"]["stop"] for r, s in states(tmp_path / "data/portfolio.db")}

    usdt.ex = BasketExchange(prices=PRICES)                  # same account balance, empty ledger
    (tmp_path / "fresh").mkdir()
    shutil.copy(CONFIG, tmp_path / "fresh" / "config.portfolio.yaml")
    monkeypatch.chdir(tmp_path / "fresh")
    multi = usdt.launch("tradingbot-usdt.service")
    multi.runners["SOL/USDT"].strategy.signal = -1
    multi._round()
    assert orders(usdt.ex) == before
    assert {r: s["trader"]["broker"]["position"]["stop"]
            for r, s in states(tmp_path / "fresh/data/portfolio.db")} == stops
    for symbol, runner in multi.runners.items():             # each still sizes off half the USDT
        price = usdt.ex.last[runner.symbol]
        assert runner.trader.broker.equity(price) == pytest.approx(50_000.0 * 0.5)


def states(db: Path) -> list[tuple[str, dict]]:
    conn = sqlite3.connect(db)
    try:
        return [(run, json.loads(data)) for run, data in conn.execute("SELECT run, data FROM state ORDER BY run")]
    finally:
        conn.close()
