"""Portfolio combine: sleeves that start at different bars must line up by timestamp whatever
the --symbols order. pd.concat(axis=1) leaves an integer union index unsorted. Every sleeve, the
basket and the trades share one window that starts where the latest sleeve starts. No network."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import portfolio_backtest as pb  # noqa: E402
from bot.config import Config, RiskConfig, StrategyConfig  # noqa: E402
from bot.engine.backtest import run_backtest  # noqa: E402
from conftest import make_ohlcv  # noqa: E402

STEP = 4 * 3600 * 1000
T0 = 1_672_531_200_000


def sleeve(start_bar, n_bars, values):
    return pd.Series(np.asarray(values, dtype=float), index=T0 + (start_bar + np.arange(n_bars)) * STEP)


@pytest.fixture
def parts():
    """BTC trades from bar 0, SOL only lists at bar 4; both run to bar 9."""
    btc_eq = sleeve(0, 10, np.linspace(500, 600, 10))
    sol_eq = sleeve(4, 6, np.linspace(500, 450, 6))
    btc_px = sleeve(0, 10, np.linspace(100, 200, 10))
    sol_px = sleeve(4, 6, np.linspace(10, 30, 6))
    return {"BTC": btc_eq, "SOL": sol_eq}, {"BTC": btc_px, "SOL": sol_px}


def test_later_listed_symbol_first_matches_live_order(parts):
    equity, close = parts
    port, basket = pb.combine(equity, close, 1000)
    port_rev, basket_rev = pb.combine(dict(reversed(equity.items())), dict(reversed(close.items())), 1000)

    assert port.index.is_monotonic_increasing and basket.index.is_monotonic_increasing
    assert port.iloc[-1] == pytest.approx(600 + 450)   # last bar of each sleeve, not an early one
    pd.testing.assert_series_equal(port_rev, port)
    pd.testing.assert_series_equal(basket_rev, basket)


def test_portfolio_starts_once_every_sleeve_has_started(parts):
    equity, close = parts
    port, _ = pb.combine(dict(reversed(equity.items())), dict(reversed(close.items())), 1000)
    assert port.index[0] == T0 + 4 * STEP
    assert len(port) == 6


def test_basket_starts_with_portfolio_and_keeps_every_coin(parts):
    equity, close = parts
    port, basket = pb.combine(equity, close, 1000)
    pd.testing.assert_index_equal(basket.index, port.index)
    assert basket.iloc[0] == pytest.approx(1000)
    btc_at_start = np.linspace(100, 200, 10)[4]
    assert basket.iloc[-1] == pytest.approx(1000 * (200 / btc_at_start + 30 / 10) / 2)   # SOL not dropped


@pytest.fixture
def staggered():
    """OLD has 600 bars of alternating trends; NEW is listed at OLD's bar 300."""
    cfg = Config(market="perp", initial_equity=500, strategy=StrategyConfig("ma_cross", {"fast": 10, "slow": 40}),
                 risk=RiskConfig(stop_atr_mult=3, max_drawdown=0.5))
    cfg.validate()
    rng = np.random.default_rng(7)
    drift = np.repeat(rng.choice([-0.002, 0.002], size=12), 50)
    closes = 100 * np.exp(np.cumsum(drift + rng.normal(0, 0.004, drift.size)))
    old = make_ohlcv(closes)
    new = make_ohlcv(closes[300:] * 0.1 + np.linspace(0, 5, 300), start_ts=int(old["timestamp"].iloc[300]))
    return cfg, old, new


def test_run_sleeves_trades_every_sleeve_from_the_latest_start(staggered):
    cfg, old, new = staggered
    res = pb.run_sleeves({"OLD": (cfg, old, None), "NEW": (cfg, new, None)})
    start = res["NEW"].equity.index[0]

    alone = run_backtest(cfg, old)   # OLD on its own trades early and has P&L by NEW's start
    assert any(t["entry_ts"] < start for t in alone.trades)
    assert alone.equity.loc[start] != pytest.approx(500, abs=1)

    for r in res.values():
        assert r.equity.index[0] == start
        assert r.equity.iloc[0] == pytest.approx(500)
        assert all(t["entry_ts"] >= start for t in r.trades)
    pd.testing.assert_series_equal(res["OLD"].equity, run_backtest(cfg, old, start_ts=start).equity)

    closes = {"OLD": old.set_index("timestamp")["close"], "NEW": new.set_index("timestamp")["close"]}
    port, basket = pb.combine({s: r.equity for s, r in res.items()},
                              {s: closes[s].reindex(r.equity.index) for s, r in res.items()}, 1000)
    assert port.index[0] == basket.index[0] == start
    assert port.iloc[0] == pytest.approx(1000) and basket.iloc[0] == pytest.approx(1000)


def test_run_sleeves_leaves_equal_starts_alone(staggered):
    cfg, old, _ = staggered
    twin = old.assign(close=old["close"] * 2, open=old["open"] * 2, high=old["high"] * 2, low=old["low"] * 2)
    res = pb.run_sleeves({"A": (cfg, old, None), "B": (cfg, twin, None)})
    pd.testing.assert_series_equal(res["A"].equity, run_backtest(cfg, old).equity)
    pd.testing.assert_series_equal(res["B"].equity, run_backtest(cfg, twin).equity)
