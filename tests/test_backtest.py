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


def test_config_validation():
    with pytest.raises(ValueError):
        make_cfg(market="spot", risk=RiskConfig(leverage=2))
    with pytest.raises(ValueError):
        make_cfg(market="perp", risk=RiskConfig(leverage=5, max_leverage=3))
    assert make_cfg(market="perp").market_symbol == "BTC/USDT:USDT"


def test_example_config_loads():
    cfg = load_config("config.example.yaml", {"market": "perp", "strategy_name": "donchian"})
    assert cfg.is_perp and cfg.strategy.name == "donchian" and cfg.strategy.params == {}
