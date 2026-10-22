import numpy as np
import pandas as pd
import pytest

from bot.config import Config, RiskConfig, StrategyConfig
from bot.engine.backtest import run_backtest
from bot.engine.portfolio import (DAY_MS, daily_close, equal_weights, inverse_vol_weights,
                                  run_portfolio, turnover)
from conftest import HOUR, make_ohlcv

T0 = 1_735_689_600_000  # 2025-01-01 00:00 UTC


def make_cfg(**kw):
    cfg = Config(market="perp", strategy=StrategyConfig("ma_cross", {"fast": 5, "slow": 20}),
                 risk=RiskConfig(stop_atr_mult=3), **kw)
    cfg.validate()
    return cfg


def random_walk(days, vol, seed):
    rng = np.random.default_rng(seed)
    return 100 * np.exp(np.cumsum(rng.normal(0, vol, days)))


def daily_prices(**cols):
    n = max(len(v) for v in cols.values())
    idx = pd.Index(T0 + np.arange(n) * DAY_MS)
    return pd.DataFrame({k: np.r_[np.full(n - len(v), np.nan), v] for k, v in cols.items()}, index=idx)


def test_inverse_vol_weights_follow_one_over_vol():
    prices = daily_prices(calm=random_walk(400, 0.01, 1), wild=random_walk(400, 0.03, 2))
    w = inverse_vol_weights(prices, lookback=60)
    assert np.allclose(w.sum(axis=1), 1.0)
    late = w.iloc[100:].mean()
    assert late["calm"] == pytest.approx(0.75, abs=0.05)  # (1/1) / (1/1 + 1/3)
    assert late["wild"] == pytest.approx(0.25, abs=0.05)
    # until a full window exists, it falls back to equal weight
    assert w.iloc[:60].eq(0.5).all().all()


def test_inverse_vol_weights_use_no_future_data():
    a, b = random_walk(200, 0.02, 3), random_walk(200, 0.02, 4)
    base = inverse_vol_weights(daily_prices(a=a, b=b), lookback=30)
    shocked_a = a.copy()
    shocked_a[150:] *= 1.5  # a huge move on day 150
    shocked = inverse_vol_weights(daily_prices(a=shocked_a, b=b), lookback=30)
    pd.testing.assert_frame_equal(base.iloc[:151], shocked.iloc[:151])  # day 150 still uses data <= day 149
    assert shocked["a"].iloc[151] < base["a"].iloc[151]


def test_inverse_vol_weights_late_listing_waits_for_a_full_window():
    prices = daily_prices(old=random_walk(300, 0.02, 5), new=random_walk(100, 0.02, 6))
    w = inverse_vol_weights(prices, lookback=60)
    listed = 200  # first day with a price for "new"
    assert w["new"].iloc[: listed + 61].eq(0).all()   # 60 returns need 61 prices, used the day after
    assert w["new"].iloc[listed + 61] > 0
    assert w["old"].iloc[: listed + 61].iloc[61:].eq(1).all()


def test_equal_weights_only_count_listed_symbols():
    w = equal_weights(daily_prices(a=np.ones(10), b=np.ones(4), c=np.ones(4)))
    assert w.iloc[0].tolist() == [1.0, 0.0, 0.0]
    assert w.iloc[-1].tolist() == pytest.approx([1 / 3] * 3)


def test_daily_close_takes_last_bar_of_each_day():
    df = make_ohlcv(np.arange(48, dtype=float) + 1, start_ts=T0)  # 48 hourly bars = 2 days
    s = daily_close(df)
    assert s.index.tolist() == [T0, T0 + DAY_MS] and s.tolist() == [24.0, 48.0]


def trend_closes():
    return np.r_[np.full(60, 100.0), np.linspace(100, 150, 100), np.linspace(150, 90, 120)]


def test_single_symbol_matches_run_backtest(trend_df):
    cfg = make_cfg()
    ref = run_backtest(cfg, trend_df)
    res = run_portfolio({"X": cfg}, {"X": (trend_df, None)},
                        equal_weights(daily_prices(X=np.ones(20))), cfg.initial_equity)
    assert np.allclose(res.equity.to_numpy(), ref.equity.to_numpy())
    assert len(res.trades) == len(ref.trades)


def test_two_identical_sleeves_at_half_weight_equal_one_full_sleeve(trend_df):
    cfg = make_cfg()
    ref = run_backtest(cfg, trend_df)
    w = equal_weights(daily_prices(A=np.ones(20), B=np.ones(20)))
    res = run_portfolio({"A": cfg, "B": cfg}, {"A": (trend_df, None), "B": (trend_df, None)},
                        w, cfg.initial_equity)
    assert res.equity.iloc[-1] == pytest.approx(ref.equity.iloc[-1], rel=1e-3)
    assert res.per_symbol["A"]["pnl"] == pytest.approx(res.per_symbol["B"]["pnl"])


def test_weights_size_entries_and_zero_weight_never_trades(trend_df):
    cfg = make_cfg()
    days = daily_prices(A=np.ones(20), B=np.ones(20))
    tilted = pd.DataFrame({"A": 0.8, "B": 0.2}, index=days.index)
    res = run_portfolio({"A": cfg, "B": cfg}, {"A": (trend_df, None), "B": (trend_df, None)},
                        tilted, cfg.initial_equity)
    qa = [t["qty"] for t in res.trades if t["symbol"] == "A"]
    qb = [t["qty"] for t in res.trades if t["symbol"] == "B"]
    assert qa[0] / qb[0] == pytest.approx(4.0, rel=1e-6)
    only_a = pd.DataFrame({"A": 1.0, "B": 0.0}, index=days.index)
    res = run_portfolio({"A": cfg, "B": cfg}, {"A": (trend_df, None), "B": (trend_df, None)},
                        only_a, cfg.initial_equity)
    assert res.per_symbol["B"]["trades"] == 0


def test_late_listing_joins_and_weights_renormalize(trend_df):
    cfg = make_cfg()
    late = trend_df.iloc[100:].reset_index(drop=True)
    w = equal_weights(daily_prices(A=np.ones(20), B=np.ones(20)))  # B already "weighted" from day 0
    res = run_portfolio({"A": cfg, "B": cfg}, {"A": (trend_df, None), "B": (late, None)},
                        w, cfg.initial_equity)
    assert res.equity.index[0] == trend_df["timestamp"].iloc[20]  # A's warm-up, not B's
    first_a = next(t for t in res.trades if t["symbol"] == "A")
    ref = run_backtest(cfg, trend_df).trades[0]
    assert first_a["qty"] == pytest.approx(ref["qty"])  # A alone gets 100% until B starts trading


def test_turnover_is_traded_notional_over_mean_equity_per_year():
    eq = pd.Series(np.full(365 * 6, 1000.0), index=np.arange(365 * 6) * 4 * HOUR)  # one year of 4h bars
    trades = [{"qty": 1.0, "entry": 100.0, "exit": 110.0}] * 10
    assert turnover(trades, eq, 4 * HOUR) == pytest.approx(10 * 210 / 1000)
