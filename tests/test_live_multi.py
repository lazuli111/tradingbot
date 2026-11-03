"""main.py testnet-multi: the 9-coin USDC basket in one process, against an in-memory fake
exchange (no network). Each coin keeps its own Trader / broker / state; one data and one
trading ccxt instance are shared."""
import logging
import math
import time
from pathlib import Path
from types import SimpleNamespace

import ccxt
import numpy as np
import pandas as pd
import pytest

import main
from bot.config import Config, load_config
from bot.engine import live
from bot.engine.live_multi import MultiRunner, SymbolTag
from bot.halt import write_halt
from bot.indicators import atr as compute_atr
from conftest import make_ohlcv
from test_order_sync import StubStrategy

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.usdc-basket.example.yaml"
BASKET = ["BTC/USDC", "SOL/USDC", "ETH/USDC", "XRP/USDC", "LTC/USDC", "BCH/USDC", "ADA/USDC",
          "LINK/USDC", "BNB/USDC"]
PERPS = [f"{s}:USDC" for s in BASKET]
BAR = 4 * 3600 * 1000
PRICES = {p: 10.0 * (i + 1) for i, p in enumerate(PERPS)}   # a different price per coin


class BasketExchange:
    """One fake USD-M account with several perps (the USDC basket unless `prices` says otherwise).
    Market orders fill at once at the coin's `last`; `broken[method]` lists the coins for which
    that call raises a network error."""

    def __init__(self, usdc: float = 9000.0, prices: dict[str, float] | None = None):
        self.last = dict(prices or PRICES)
        self.pos = dict.fromkeys(self.last, 0.0)     # signed contracts
        self.entry = dict.fromkeys(self.last, 0.0)
        self.balance = {"USDC": usdc, "USDT": 50_000.0}   # the USDT belongs to the other bots
        self.orders: list[dict] = []
        self.broken: dict[str, set] = {}

    def _maybe_fail(self, method, symbol):
        if symbol in self.broken.get(method, ()):
            raise ccxt.NetworkError(f"{method} {symbol}: timeout")

    def load_markets(self):
        return {}

    def market(self, symbol):
        if symbol not in self.last:
            raise ccxt.BadSymbol(f"binance does not have market symbol {symbol}")
        settle = symbol.split(":")[1]
        return {"base": symbol.split("/")[0], "quote": settle, "settle": settle, "contractSize": 1,
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
        self._maybe_fail("fetch_ticker", symbol)
        return {"last": self.last[symbol]}

    def fetch_balance(self):
        return {c: {"total": v} for c, v in self.balance.items()}

    def create_order(self, symbol, type_, side, amount, price, params):
        self._maybe_fail("create_order", symbol)
        signed, pos, last = (amount if side == "buy" else -amount), self.pos[symbol], self.last[symbol]
        if params.get("reduceOnly") and (pos == 0 or pos * signed > 0 or abs(signed) > abs(pos) + 1e-12):
            raise ccxt.InvalidOrder('binance {"code":-2022,"msg":"ReduceOnly Order is rejected."}')
        if pos == 0 or pos * signed > 0:
            self.entry[symbol] = (abs(pos) * self.entry[symbol] + amount * last) / (abs(pos) + amount)
        self.pos[symbol] = round(pos + signed, 9)
        oid = str(len(self.orders) + 1)
        self.orders.append({"id": oid, "symbol": symbol, "side": side, "amount": amount, "params": params})
        return {"id": oid, "status": "closed", "average": last, "filled": amount,
                "fee": {"cost": amount * last * 0.0005, "currency": symbol.split(":")[1]}}

    def fetch_positions(self, symbols):
        out = []
        for s in symbols:
            self._maybe_fail("fetch_positions", s)
            if self.pos[s]:
                out.append({"side": "long" if self.pos[s] > 0 else "short", "contracts": abs(self.pos[s]),
                            "entryPrice": self.entry[s], "timestamp": 0, "initialMargin": 0,
                            "liquidationPrice": None})
        return out

    def orders_for(self, symbol):
        return [o for o in self.orders if o["symbol"] == symbol]


class Chart:
    """Stands in for fetch_ohlcv_range: closed 4h bars per coin, served from `since` on."""

    def __init__(self, prices: dict[str, float] | None = None):
        start = (int(time.time() * 1000) // BAR - 60) * BAR
        # wide bars (±2%) so the risk-based size, not the leverage cap, sets the quantity
        self.bars = {p: make_ohlcv(np.full(60, price), start_ts=start, spread=0.02).assign(
            timestamp=lambda d: start + np.arange(len(d)) * BAR) for p, price in (prices or PRICES).items()}

    def __call__(self, ex, symbol, timeframe, since, until):
        df = self.bars[symbol]
        return df[df["timestamp"] >= since].reset_index(drop=True)

    def close_bar(self, ex, symbols=None):
        """The next bar closes for these coins (all of them by default), at their current `last`."""
        for p in symbols or list(self.bars):
            df = self.bars[p]
            nxt = make_ohlcv([ex.last[p]], start_ts=int(df["timestamp"].iloc[-1]) + BAR, spread=0.02)
            self.bars[p] = pd.concat([df, nxt], ignore_index=True)


@pytest.fixture
def basket(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)                 # db_path / halt_file in the config are relative
    monkeypatch.setattr("bot.broker.ccxt_broker.time.sleep", lambda s: None)
    ex, chart, calls = BasketExchange(), Chart(), []

    def make_exchange(*a, **k):
        calls.append((a, k))
        return ex

    monkeypatch.setattr(live, "make_exchange", make_exchange)
    monkeypatch.setattr(live, "api_credentials", lambda _: {"apiKey": "k", "secret": "s"})
    monkeypatch.setattr(live, "create_strategy", lambda *a: StubStrategy())
    monkeypatch.setattr(live, "fetch_ohlcv_range", chart)
    cfg = load_config(CONFIG)
    return SimpleNamespace(ex=ex, chart=chart, calls=calls, cfg=cfg, tmp=tmp_path,
                           make=lambda: MultiRunner(cfg, "testnet"))


def set_signals(multi, signals: dict[str, int]):
    for symbol, signal in signals.items():
        multi.runners[symbol].strategy.signal = signal


def alternating():
    return {s: 1 if i % 2 == 0 else -1 for i, s in enumerate(BASKET)}


def perp(symbol):
    return f"{symbol}:USDC"


# ---- config ----------------------------------------------------------------------

def test_example_config_is_the_usdc_basket():
    cfg = load_config(CONFIG)
    assert cfg.symbols == BASKET
    assert (cfg.market, cfg.timeframe, cfg.initial_equity) == ("perp", "4h", 5000)
    assert cfg.strategy.name == "ma_cross" and cfg.strategy.params == {"fast": 20, "slow": 50, "adx_min": 20}
    r = cfg.risk
    assert (r.risk_per_trade, r.leverage, r.max_leverage, r.stop_atr_mult) == (0.03, 3, 3, 2.0)
    assert r.take_profit_atr_mult is None and (r.daily_loss_limit, r.max_drawdown) == (0.03, 0.99)
    assert cfg.live.equity_fraction == pytest.approx(1 / 9, abs=1e-6)
    assert cfg.live.equity_fraction * 9 <= 1
    assert (cfg.live.db_path, cfg.live.halt_file) == ("data/usdc_basket.db", "data/HALT_USDC")
    portfolio = load_config(ROOT / "config.portfolio.example.yaml")
    assert cfg.strategy == portfolio.strategy               # same rules as the USDT bots


def test_config_rejects_bad_symbol_lists():
    def check(symbols, fraction):
        cfg = Config(market="perp", symbols=symbols)
        cfg.live.equity_fraction = fraction
        cfg.validate()

    check(["BTC/USDC", "SOL/USDC"], 0.5)
    for symbols, fraction, why in ((["BTC/USDC", "BTC/USDC:USDC"], 0.5, "重复"),
                                   (["BTC/USDC", "SOL/USDT"], 0.5, "计价币"),
                                   (["BTC/USDC", "SOL/USDC", "ETH/USDC"], 0.5, "超过账户权益"),
                                   (["BTCUSDC"], 1.0, "BASE/QUOTE")):
        with pytest.raises(ValueError, match=why):
            check(symbols, fraction)
    for path in ROOT.glob("config*.example.yaml"):          # single-coin configs: nothing new to say
        load_config(path)


# ---- one process, nine coins ---------------------------------------------------------

def test_one_ccxt_instance_each_for_data_and_trading(basket):
    multi = basket.make()
    assert [k.get("testnet", False) for _, k in basket.calls] == [False, True]   # 9 coins, 2 instances
    assert list(multi.runners) == BASKET
    for s, runner in multi.runners.items():
        assert runner.symbol == perp(s)
        assert runner.data_ex is multi.data_ex
        assert runner.trader.broker._inner.ex is multi.trade_ex
        assert runner.trader.halt_check.args == ("data/HALT_USDC",)
    runs = [r.storage.run for r in multi.runners.values()]
    assert len(set(runs)) == 9 and all(perp(s) in run for s, run in zip(BASKET, runs))


def test_single_coin_runner_still_makes_its_own_exchanges(basket):
    cfg = Config(exchange="binance", market="perp", symbol="BTC/USDC")
    cfg.live.db_path = str(basket.tmp / "single.db")
    live.LiveRunner(cfg, "testnet")
    assert basket.calls == [(("binance", "perp"), {}),
                            (("binance", "perp", {"apiKey": "k", "secret": "s"}), {"testnet": True})]


def test_each_coin_opens_and_closes_its_own_position(basket):
    ex = basket.ex
    multi = basket.make()
    set_signals(multi, alternating())
    multi._round()
    for s, signal in alternating().items():
        assert math.copysign(1, ex.pos[perp(s)]) == signal
        assert multi.runners[s].trader.broker.position.side == signal
        assert [o["side"] for o in ex.orders_for(perp(s))] == ["buy" if signal == 1 else "sell"]

    flipped = BASKET[:3]                                     # BTC long -> short, SOL short -> long, ...
    set_signals(multi, {s: -alternating()[s] for s in flipped})
    basket.chart.close_bar(ex)
    assert multi._round() is True
    for s in BASKET:
        want = -alternating()[s] if s in flipped else alternating()[s]
        assert math.copysign(1, ex.pos[perp(s)]) == want
        assert multi.runners[s].trader.broker.position.side == want
        orders = ex.orders_for(perp(s))
        assert len(orders) == (3 if s in flipped else 1)
        if s in flipped:                                     # reduceOnly close, then the open
            assert orders[1]["params"] == {"reduceOnly": True} and orders[2]["params"] == {}
        fills = multi.runners[s].storage.fills()
        assert [f[6] for f in fills] == ["signal"] * len(orders)
    assert multi._round() is False                           # nothing more to do
    assert len(ex.orders) == 9 + 2 * 3


def test_each_coin_sizes_off_a_ninth_of_the_usdc_equity(basket):
    ex = basket.ex
    multi = basket.make()
    multi._round()
    share = 9000.0 * basket.cfg.live.equity_fraction         # USDC only: the 50k USDT isn't ours
    for s, runner in multi.runners.items():
        price = ex.last[perp(s)]
        assert runner.trader.broker.equity(price) == pytest.approx(share)
        atr = float(compute_atr(runner.df, 14).iloc[-1])
        qty = share * 0.03 / (2.0 * atr)
        assert qty * price < share * 3                       # risk sets the size, not the 3x cap
        [order] = ex.orders_for(perp(s))
        assert order["amount"] == pytest.approx(math.floor(qty * 1000 + 1e-9) / 1000)
        assert runner.trader.broker.position.stop == pytest.approx(price - 2.0 * atr)


def test_one_coin_failing_does_not_stop_the_others(basket):
    ex = basket.ex
    sol, eth = "SOL/USDC", "ETH/USDC"
    multi = basket.make()
    ex.broken = {"create_order": {perp(sol)}, "fetch_positions": {perp(eth)}}   # an order / a reconcile fails
    multi._round()                                           # the latest closed bar, on start
    for s in BASKET:
        opened = s not in (sol, eth)
        assert (ex.pos[perp(s)] != 0) is opened
        assert multi.runners[s]._needs_reconcile is not opened
    assert {sol, eth} <= multi.started

    ex.broken = {}                                           # 30 s later: both recover on their own
    multi._round()
    assert all(ex.pos[p] > 0 for p in PERPS)
    assert all(len(ex.orders_for(p)) == 1 for p in PERPS)    # nothing sent twice
    assert not any(r._needs_reconcile or r._bar_unfinished for r in multi.runners.values())


def test_coin_that_fails_to_start_is_retried(basket):
    ex = basket.ex
    ada = "ADA/USDC"
    ex.broken = {"fetch_positions": {perp(ada)}}
    multi = basket.make()
    assert ada not in multi.runners and len(multi.runners) == 8
    multi._round()
    assert ada not in multi.runners and all(ex.pos[perp(s)] > 0 for s in BASKET if s != ada)
    ex.broken = {}
    multi._round()
    assert ada in multi.started and ex.pos[perp(ada)] > 0
    assert len(ex.orders) == 9


def test_a_misspelled_coin_stops_the_start(basket):
    basket.cfg.symbols = ["BTC/USDC", "BTCC/USDC"]
    with pytest.raises(ccxt.BadSymbol):
        basket.make()


def test_no_usdc_balance_stops_the_start(basket):
    del basket.ex.balance["USDC"]                            # ccxt skips assets with updateTime 0
    with pytest.raises(RuntimeError, match="USDC"):
        basket.make()


def test_halt_usdc_blocks_new_positions_for_every_coin(basket):
    ex = basket.ex
    write_halt("USDT 组合已停机", basket.tmp / "data" / "HALT")   # the USDT bots' flag: not ours
    multi = basket.make()
    multi._round()
    assert all(ex.pos[p] > 0 for p in PERPS)

    write_halt("USDC 组合回撤 -20.10%（阈值 20%）", basket.tmp / "data" / "HALT_USDC")
    set_signals(multi, dict.fromkeys(BASKET, -1))
    basket.chart.close_bar(ex)
    multi._round()
    assert all(ex.pos[p] == 0 for p in PERPS)                # the longs close, no short opens
    assert all(r.trader.broker.position is None for r in multi.runners.values())
    assert [o["params"] for o in ex.orders[9:]] == [{"reduceOnly": True}] * 9
    basket.chart.close_bar(ex)
    multi._round()
    assert len(ex.orders) == 18


def test_restart_restores_each_coin_from_its_own_state(basket):
    ex = basket.ex
    multi = basket.make()
    set_signals(multi, alternating())
    multi._round()
    before = {s: (r.trader.last_signal, r.trader.broker.position.stop, r.trader.risk.peak_equity)
              for s, r in multi.runners.items()}
    assert len({stop for _, stop, _ in before.values()}) == 9
    multi._save_all()

    again = basket.make()                                    # e.g. systemd restarted the service
    set_signals(again, alternating())
    for s, runner in again.runners.items():
        t = runner.trader
        assert (t.last_signal, t.broker.position.stop, t.risk.peak_equity) == before[s]
        assert t.broker.position.side == alternating()[s]
    again._round()                                           # re-acts on the latest bar: same signal
    assert len(ex.orders) == 9
    assert all(len(r.storage.fills()) == 1 for r in again.runners.values())


def test_run_loop_saves_and_tags_log_lines(basket, monkeypatch, caplog):
    ex = basket.ex
    multi = basket.make()
    monkeypatch.setattr("bot.engine.live_multi.time.sleep", lambda s: basket.chart.close_bar(ex))
    caplog.handler.addFilter(SymbolTag())
    with caplog.at_level(logging.INFO):
        multi.run(max_bars=1)
    closes = [r for r in caplog.records if r.getMessage().startswith("K 线收盘")]
    assert sorted(r.symbol for r in closes) == sorted(BASKET * 2)   # on start + one new bar, per coin
    assert all(r.symbol == "-" for r in caplog.records if r.getMessage().startswith("启动 testnet"))
    for runner in multi.runners.values():
        assert runner.storage.load_state()["trader"]["last_signal"] == 1


def test_cli_passes_symbols_to_the_multi_runner(monkeypatch):
    seen = {}

    class Fake:
        def __init__(self, cfg, mode):
            seen.update(symbols=cfg.symbols, mode=mode, halt=cfg.live.halt_file)

        def run(self, max_bars):
            seen["max_bars"] = max_bars

    monkeypatch.setattr("bot.engine.live_multi.MultiRunner", Fake)
    assert main.main(["testnet-multi", "--config", str(CONFIG), "--symbols", "BTC/USDC, SOL/USDC",
                      "--max-bars", "2"]) == 0
    assert seen == {"symbols": ["BTC/USDC", "SOL/USDC"], "mode": "testnet", "halt": "data/HALT_USDC",
                    "max_bars": 2}
    seen.clear()
    assert main.main(["testnet-multi", "--config", str(CONFIG)]) == 0
    assert seen["symbols"] == BASKET
    assert main.main(["testnet-multi", "--config", str(ROOT / "config.portfolio.example.yaml")]) == 2


def test_usdc_unit_runs_the_basket_next_to_the_usdt_bots():
    unit = (ROOT / "deploy/systemd/tradingbot-usdc.service").read_text(encoding="utf-8")
    btc = (ROOT / "deploy/systemd/tradingbot-btc.service").read_text(encoding="utf-8")
    assert "main.py testnet-multi --config config.usdc-basket.yaml" in unit
    assert "OOMScoreAdjust=500" in unit and "MemoryMax=" in unit
    assert unit.count("append:/var/log/tradingbot-usdc.log") == 2
    for line in ("User=tradingbot", "WorkingDirectory=/opt/tradingbot", "Restart=always",
                 "OPENBLAS_NUM_THREADS=1"):
        assert line in unit and line in btc
