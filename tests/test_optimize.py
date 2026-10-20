import math

import numpy as np
import pytest

from bot.config import Config, RiskConfig, StrategyConfig
from bot.engine.backtest import run_backtest
from bot.engine.optimize import grid_search, make_windows, param_combos, walk_forward
from conftest import HOUR, make_ohlcv
from main import parse_grid

DAY = 24 * HOUR
GRID = {"fast": [5, 10, 40], "slow": [20, 40]}   # includes the invalid fast=40/slow=20,40


def make_cfg():
    cfg = Config(market="perp", strategy=StrategyConfig("ma_cross", {"fast": 10, "slow": 40}),
                 risk=RiskConfig(stop_atr_mult=3, max_drawdown=0.5))
    cfg.validate()
    return cfg


@pytest.fixture
def walk_df():
    rng = np.random.default_rng(7)
    drift = np.repeat(rng.choice([-0.002, 0.002], size=30), 50)   # alternating trends
    closes = 100 * np.exp(np.cumsum(drift + rng.normal(0, 0.004, drift.size)))
    return make_ohlcv(closes)


def test_param_combos_and_parse_grid():
    assert len(param_combos(GRID)) == 6
    assert parse_grid("fast=5,10; slow=20,40.5") == {"fast": [5, 10], "slow": [20, 40.5]}


def test_make_windows_roll_by_test_length():
    w = make_windows(0, 100 * DAY, 30 * DAY, 20 * DAY)
    assert w[0] == (0, 30 * DAY, 50 * DAY)
    assert w[1][0] == 20 * DAY                      # rolled forward by one test window
    assert w[-1][2] == 100 * DAY                    # last test window clipped to the data
    assert all(test_start == train_start + 30 * DAY for train_start, test_start, _ in w)


def test_start_ts_only_trades_after_start(walk_df):
    start = int(walk_df["timestamp"].iloc[500])
    res = run_backtest(make_cfg(), walk_df, start_ts=start)
    assert res.equity.index[0] == start
    assert all(t["entry_ts"] >= start for t in res.trades)


def test_grid_search_skips_invalid_and_sorts(walk_df):
    cands = grid_search(make_cfg(), walk_df, GRID, min_trades=1)
    assert len(cands) == 4                          # fast=40 is never < slow, so skipped
    assert all(p.params["fast"] < p.params["slow"] for p in cands)
    scores = [c.score for c in cands]
    assert scores == sorted(scores, reverse=True)


def test_walk_forward_uses_only_past_data(walk_df):
    cfg = make_cfg()
    wf = walk_forward(cfg, walk_df, GRID, train_days=20, test_days=10, min_trades=1)
    assert len(wf.windows) >= 3
    eq_idx = wf.equity.index.to_numpy()
    assert (np.diff(eq_idx) > 0).all() and eq_idx[0] == wf.windows[0].test_start

    # rewrite everything after the first training window: its parameter choice must not change
    future = walk_df["timestamp"] >= wf.windows[0].test_start
    tampered = walk_df.copy()
    tampered.loc[future, ["open", "high", "low", "close"]] = tampered.loc[future, ["open", "high", "low", "close"]][::-1].to_numpy()
    wf2 = walk_forward(cfg, tampered, GRID, train_days=20, test_days=10, min_trades=1)
    assert wf2.windows[0].params == wf.windows[0].params
    assert wf2.windows[0].train_score == wf.windows[0].train_score


def test_walk_forward_compounds_window_returns(walk_df):
    wf = walk_forward(make_cfg(), walk_df, GRID, train_days=20, test_days=10, min_trades=1)
    compounded = math.prod(1 + w.test_return for w in wf.windows) - 1
    assert wf.metrics["total_return"] == pytest.approx(compounded)


def test_unknown_grid_param_rejected(walk_df):
    with pytest.raises(ValueError, match="没有参数"):
        walk_forward(make_cfg(), walk_df, {"period": [5]}, train_days=20, test_days=10)


def test_walk_forward_accepts_filter_grid(walk_df):
    grid = {"fast": [5, 10], "slow": [40], "adx_min": [0, 20]}
    wf = walk_forward(make_cfg(), walk_df, grid, train_days=20, test_days=10, min_trades=1)
    chosen = [w.params for w in wf.windows if w.params]
    assert chosen and all(set(p) == {"fast", "slow", "adx_min"} for p in chosen)
