"""Strategies re-implemented from open-source projects (see docs/research/oss_strategies.md)."""
import numpy as np
import pandas as pd
import pytest

from bot.strategies import STRATEGIES, create_strategy
from bot.strategies.dual_thrust import anchor_open
from conftest import HOUR, make_ohlcv

NEW = ["rsi2", "bb_reversion", "bb_breakout", "dual_thrust", "vol_system"]


def noisy_df(n=600, seed=7, drift=0.0):
    rng = np.random.default_rng(seed)
    closes = 100 * np.exp(np.cumsum(drift + rng.normal(0, 0.01, n)))
    return make_ohlcv(closes, spread=0.004)


def test_rsi2_buys_a_dip_in_an_uptrend():
    closes = np.r_[np.linspace(100, 150, 120), [148, 145, 141], np.linspace(143, 152, 6)]
    sig = create_strategy("rsi2", {"trend": 50, "exit": 5}).signals(make_ohlcv(closes))
    assert (sig.iloc[:120] == 0).all()           # warm-up, then a smooth rally: never oversold
    assert sig.iloc[122] == 1                     # three down closes above the 50-bar SMA
    assert sig.iloc[-1] == 0                      # back above the 5-bar SMA -> out


def test_rsi2_shorts_a_bounce_in_a_downtrend():
    closes = np.r_[np.linspace(150, 100, 120), [102, 105, 109], np.linspace(107, 98, 6)]
    sig = create_strategy("rsi2", {"trend": 50, "exit": 5}).signals(make_ohlcv(closes))
    assert sig.iloc[122] == -1
    assert sig.iloc[-1] == 0


def test_bb_reversion_fades_band_breaks():
    flat = np.r_[np.tile([100.0, 101.0], 30)]
    down = make_ohlcv(np.r_[flat, [95.0, 96.0, 100.5, 100.5]])
    sig = create_strategy("bb_reversion", {"length": 20}).signals(down)
    assert (sig.iloc[:60] == 0).all()
    assert sig.iloc[60] == 1                      # close far below the lower band
    assert sig.iloc[-1] == 0                      # back above the middle band
    up = make_ohlcv(np.r_[flat, [106.0, 105.0]])
    assert create_strategy("bb_reversion", {"length": 20}).signals(up).iloc[60] == -1


def test_bb_breakout_follows_the_break_and_exits_at_the_middle():
    closes = np.r_[np.tile([100.0, 100.5], 60), np.linspace(101, 112, 6), np.linspace(111, 99, 8)]
    sig = create_strategy("bb_breakout", {"length": 20, "cloud": 0}).signals(make_ohlcv(closes))
    assert sig.iloc[121] == 1
    assert sig.iloc[-1] in (0, -1)
    falling = np.r_[np.tile([100.0, 100.5], 60), np.linspace(99.5, 88, 6)]
    assert create_strategy("bb_breakout", {"length": 20, "cloud": 0}).signals(make_ohlcv(falling)).iloc[121] == -1


def test_bb_breakout_cloud_blocks_counter_trend_entries():
    # long decline, then a pop above the upper band while price is still under the cloud
    closes = np.r_[np.linspace(160, 100, 150), np.tile([100.0, 100.5], 15), [106.0]]
    df = make_ohlcv(closes)
    assert create_strategy("bb_breakout", {"cloud": 0}).signals(df).iloc[-1] == 1
    assert create_strategy("bb_breakout", {"cloud": 1}).signals(df).iloc[-1] == 0


def test_bb_breakout_squeeze_requires_a_quiet_spell():
    rng = np.random.default_rng(3)
    wild = 200 + np.cumsum(rng.normal(0, np.linspace(0.5, 4, 200)))   # volatility keeps rising
    quiet = np.r_[wild, np.full(30, wild[-1]) + np.tile([0, 0.1], 15)]
    jump = quiet[-1] * 1.05
    no_squeeze = make_ohlcv(np.r_[wild, [wild[-1] * 1.2]])
    squeeze = {"cloud": 0, "squeeze_pct": 0.1}
    assert create_strategy("bb_breakout", squeeze).signals(make_ohlcv(np.r_[quiet, [jump]])).iloc[-1] == 1
    assert create_strategy("bb_breakout", squeeze).signals(no_squeeze).iloc[-1] == 0


def test_dual_thrust_stop_and_reverse():
    day = 24 * HOUR
    closes = np.r_[np.tile([100.0, 100.2], 30), [110.0], np.full(5, 105.0), [92.0]]
    df = make_ohlcv(closes)
    df["timestamp"] = 1_735_689_600_000 + np.arange(len(df)) * 4 * HOUR   # 4h bars, daily anchor
    sig = create_strategy("dual_thrust", {"lookback": 6, "k": 1.0}).signals(df)
    assert (sig.iloc[:7] == 0).all()
    assert sig.iloc[60] == 1
    assert (sig.iloc[61:66] == 1).all()          # no exit until the opposite trigger
    assert sig.iloc[66] == -1
    assert (anchor_open(df)[df["timestamp"] % day == 0] == df["open"][df["timestamp"] % day == 0]).all()


def test_dual_thrust_anchor_is_period_start():
    df = make_ohlcv(np.linspace(100, 110, 12))   # 1h bars -> 4h anchor
    anchors = anchor_open(df)
    first = df["timestamp"] // (4 * HOUR) * (4 * HOUR)
    expected = df.groupby(first)["open"].transform("first").to_numpy()
    assert (anchors == expected).all()


def test_vol_system_enters_on_a_volatility_shock():
    closes = np.r_[np.tile([100.0, 100.4], 20), [104.0], np.full(5, 104.0), [99.0]]
    sig = create_strategy("vol_system", {"atr_mult": 2.0}).signals(make_ohlcv(closes))
    assert (sig.iloc[:40] == 0).all()
    assert sig.iloc[40] == 1
    assert (sig.iloc[41:46] == 1).all()
    assert sig.iloc[46] == -1


@pytest.mark.parametrize("name", NEW)
def test_warmup_is_flat_and_covers_indicators(name):
    # vol_system's default 2 x ATR shock almost never happens on Gaussian noise
    s = create_strategy(name, {"atr_mult": 1.0, "change_bars": 3} if name == "vol_system" else {})
    df = noisy_df()
    sig = s.signals(df)
    assert (sig.iloc[:s.warmup - 1] == 0).all()
    assert set(np.unique(sig)) <= {-1, 0, 1}
    assert (sig != 0).any()                      # trades at all on random-walk data


@pytest.mark.parametrize("name", NEW)
def test_no_lookahead(name):
    """Every grid combination: signals must not change when future bars are appended,
    nor when the future is replaced by something else."""
    df = noisy_df(n=500)
    for params in [dict(zip(STRATEGIES[name].param_grid, v))
                   for v in __import__("itertools").product(*STRATEGIES[name].param_grid.values())]:
        s = create_strategy(name, params)
        full = s.signals(df).to_numpy()
        for cut in (260, 380):
            assert (s.signals(df.iloc[:cut]).to_numpy() == full[:cut]).all(), params
            tampered = df.copy()
            tampered.loc[cut:, ["open", "high", "low", "close"]] *= 1.5
            assert (s.signals(tampered).to_numpy()[:cut] == full[:cut]).all(), params


@pytest.mark.parametrize("name,params", [
    ("rsi2", {"exit": 200, "trend": 100}), ("rsi2", {"oversold": 60}),
    ("bb_reversion", {"exit_pctb": 0.3}), ("bb_breakout", {"cloud": 2}),
    ("dual_thrust", {"k": 0}), ("vol_system", {"change_bars": 0}),
])
def test_bad_params_rejected(name, params):
    with pytest.raises(ValueError):
        create_strategy(name, params)


@pytest.mark.parametrize("name", NEW)
def test_grid_is_small_and_valid(name):
    grid = STRATEGIES[name].param_grid
    combos = int(np.prod([len(v) for v in grid.values()]))
    assert combos <= 30
    for v in __import__("itertools").product(*grid.values()):
        create_strategy(name, dict(zip(grid, v)))   # every combination is valid


def test_runs_in_backtest():
    from bot.config import Config, StrategyConfig
    from bot.engine.backtest import run_backtest
    df = noisy_df(n=800)
    for name in NEW:
        cfg = Config(market="perp", strategy=StrategyConfig(name, {}))
        res = run_backtest(cfg, df)
        assert isinstance(res.equity, pd.Series) and len(res.equity) > 0


def test_rsi_and_bollinger_indicators():
    from bot.indicators import bollinger, rsi
    up = pd.Series(np.linspace(1, 2, 30))
    assert rsi(up, 2).iloc[-1] == pytest.approx(100)
    assert rsi(up[::-1].reset_index(drop=True), 2).iloc[-1] == pytest.approx(0)
    assert rsi(pd.Series(np.full(10, 5.0)), 2).iloc[-1] == 50
    lower, mid, upper = bollinger(pd.Series([1.0, 3.0] * 10), 4, 2.0)
    assert mid.iloc[-1] == 2 and upper.iloc[-1] == 4 and lower.iloc[-1] == 0   # population std = 1
