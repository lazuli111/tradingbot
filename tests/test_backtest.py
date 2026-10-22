import numpy as np
import pandas as pd
import pytest

from bot.config import Config, RiskConfig, StrategyConfig, load_config
from bot.engine.backtest import funding_per_bar, run_backtest
from conftest import HOUR, make_ohlcv


def make_cfg(**kw):
    risk = kw.pop("risk", RiskConfig(stop_atr_mult=3))
    cfg = Config(strategy=StrategyConfig("ma_cross", {"fast": 5, "slow": 20}), risk=risk, **kw)
    cfg.validate()
    return cfg


def test_spot_profits_on_rally_and_stays_flat_on_decline(trend_df):
    res = run_backtest(make_cfg(market="spot"), trend_df)
    assert res.metrics["trades"] >= 1
    assert all(t["side"] == "long" for t in res.trades)
    assert res.trades[0]["pnl"] > 0


def test_perp_shorts_the_decline(trend_df):
    res = run_backtest(make_cfg(market="perp"), trend_df)
    shorts = [t for t in res.trades if t["side"] == "short"]
    assert shorts and sum(t["pnl"] for t in shorts) > 0


def test_entry_is_next_bar_open(trend_df):
    res = run_backtest(make_cfg(market="spot", slippage=0), trend_df)
    first = res.trades[0]
    row = trend_df.index[trend_df["timestamp"] == first["entry_ts"]][0]
    assert first["entry"] == pytest.approx(trend_df["open"].iloc[row])


def test_stop_loss_fires_on_crash():
    closes = np.r_[np.full(30, 100.0), np.linspace(100, 120, 30)]
    df = make_ohlcv(closes)
    crash = df.iloc[[-1]].copy()
    crash["timestamp"] += HOUR
    crash[["open", "high", "low", "close"]] = [120, 120, 80, 85]
    df = pd.concat([df, crash], ignore_index=True)
    res = run_backtest(make_cfg(market="spot"), df)
    assert res.trades[-1]["reason"] == "stop_loss"
    loss = -res.trades[-1]["pnl"] / 10_000
    assert loss == pytest.approx(0.01, abs=0.003)  # ~1% risk per trade


def test_funding_mapped_to_bars():
    df = make_ohlcv(np.full(10, 100.0))
    t0 = int(df["timestamp"].iloc[0])
    funding = pd.DataFrame({"timestamp": [t0 - HOUR, t0, t0 + 3 * HOUR + 1, t0 + 3 * HOUR + 5],
                            "rate": [9.0, 0.1, 0.2, 0.3]})
    out = funding_per_bar(df, funding)
    assert out[0] == pytest.approx(0.1) and out[3] == pytest.approx(0.5) and out.sum() == pytest.approx(0.6)


def test_funding_after_the_last_bar_is_dropped():
    """A slice of the history (a walk-forward window) gets the full funding table: settlements
    after the slice ends must not pile up on its last bar."""
    df = make_ohlcv(np.full(10, 100.0))
    t_last = int(df["timestamp"].iloc[-1])
    funding = pd.DataFrame({"timestamp": [t_last, t_last + HOUR - 1, t_last + HOUR, t_last + 50 * HOUR],
                            "rate": [0.1, 0.2, 5.0, 7.0]})
    out = funding_per_bar(df, funding)
    assert out[-1] == pytest.approx(0.3) and out.sum() == pytest.approx(0.3)
    assert funding_per_bar(df, funding, HOUR)[-1] == pytest.approx(0.3)


def test_walk_forward_slice_funding_matches_trimmed_table():
    """Backtesting a slice with the full funding table == with the table cut to the slice."""
    from bot.config import Config
    closes = 100 * np.exp(np.cumsum(np.random.default_rng(1).normal(0, 0.01, 400)))
    df = make_ohlcv(closes)
    ts = df["timestamp"].to_numpy()
    funding = pd.DataFrame({"timestamp": np.arange(ts[0], ts[-1] + 1000 * HOUR, 8 * HOUR), "rate": 0.001})
    cfg = Config(market="perp", timeframe="1h")
    part = df.iloc[:250]
    full = run_backtest(cfg, part, funding)
    trimmed = run_backtest(cfg, part, funding[funding["timestamp"] <= ts[249]])
    assert full.metrics["funding"] == pytest.approx(trimmed.metrics["funding"])
    assert full.equity.iloc[-1] == pytest.approx(trimmed.equity.iloc[-1])


def test_config_validation():
    with pytest.raises(ValueError):
        make_cfg(market="spot", risk=RiskConfig(leverage=2))
    with pytest.raises(ValueError):
        make_cfg(market="perp", risk=RiskConfig(leverage=5, max_leverage=3))
    assert make_cfg(market="perp").market_symbol == "BTC/USDT:USDT"


def test_example_config_loads():
    cfg = load_config("config.example.yaml", {"market": "perp", "strategy_name": "donchian"})
    assert cfg.is_perp and cfg.strategy.name == "donchian" and cfg.strategy.params == {}
