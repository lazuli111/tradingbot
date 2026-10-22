import io
import zipfile

import numpy as np
import pandas as pd
import pytest

from bot.research import universe as uv
from bot.research import xsection as xs

D0 = pd.Timestamp("2024-01-01")


def make_panels(prices: dict[str, list[float]], funding: dict[str, list[float]] | None = None,
                start=D0) -> dict[str, pd.DataFrame]:
    """开盘价 = 收盘价 = 给定序列（NaN 表示没有数据）。"""
    idx = pd.date_range(start, periods=len(next(iter(prices.values()))), freq="D")
    px = pd.DataFrame(prices, index=idx, dtype=float)
    fund = pd.DataFrame(funding or {}, index=idx, dtype=float).reindex(columns=px.columns).fillna(0.0)
    return {"open": px, "close": px, "high": px, "low": px, "quote_volume": px * 0 + 1e6,
            "funding": fund}


def weights_on(idx, day: int, row: dict) -> pd.DataFrame:
    w = pd.DataFrame(np.nan, index=idx, columns=list(row))
    w.iloc[day] = pd.Series(row)
    return w


# ---------------------------------------------------------------- 回测引擎

def test_fees_charged_on_turnover_at_next_open():
    p = make_panels({"A": [100] * 5, "B": [50] * 5})
    w = weights_on(p["open"].index, 0, {"A": 0.5, "B": -0.5})
    res = xs.run(w, p, costs=xs.Costs(fee=0.0005, slip_other=0.0005, funding=False))
    # 第 0 天收盘的目标在第 1 天开盘执行：成交 10000，费用 0.1%
    assert res.daily["fees"].iloc[0] == 0
    assert res.daily["fees"].iloc[1] == pytest.approx(10.0)
    assert res.daily["turnover"].iloc[1] == pytest.approx(10_000)
    assert res.equity.iloc[-1] == pytest.approx(9_990.0)
    assert res.daily["n_long"].iloc[2] == 1 and res.daily["n_short"].iloc[2] == 1


def test_pnl_uses_open_to_open_returns_and_sign():
    p = make_panels({"A": [100, 100, 110, 110], "B": [100, 100, 110, 110]})
    w = weights_on(p["open"].index, 0, {"A": 0.5, "B": -0.5})
    res = xs.run(w, p, costs=xs.Costs(fee=0, slip_other=0, funding=False))
    # 两个都涨 10%：多头赚 500，空头亏 500
    assert res.daily["pnl"].iloc[1] == pytest.approx(0.0)
    w2 = weights_on(p["open"].index, 0, {"A": 1.0})
    assert xs.run(w2, p, costs=xs.Costs(fee=0, slip_other=0, funding=False)).equity.iloc[-1] == pytest.approx(11_000)
    w3 = weights_on(p["open"].index, 0, {"A": -1.0})
    assert xs.run(w3, p, costs=xs.Costs(fee=0, slip_other=0, funding=False)).equity.iloc[-1] == pytest.approx(9_000)


def test_funding_paid_by_longs_received_by_shorts():
    p = make_panels({"A": [100] * 4}, funding={"A": [0.001] * 4})
    c = xs.Costs(fee=0, slip_other=0)
    long_ = xs.run(weights_on(p["open"].index, 0, {"A": 1.0}), p, costs=c)
    short = xs.run(weights_on(p["open"].index, 0, {"A": -1.0}), p, costs=c)
    # 第 1~3 天持仓，每天 0.1%
    assert long_.daily["funding"].sum() == pytest.approx(30.0)
    assert long_.equity.iloc[-1] == pytest.approx(10_000 - 30)
    assert short.equity.iloc[-1] == pytest.approx(10_000 + 30)
    neg = make_panels({"A": [100] * 4}, funding={"A": [-0.001] * 4})
    assert xs.run(weights_on(neg["open"].index, 0, {"A": 1.0}), neg, costs=c).equity.iloc[-1] \
        == pytest.approx(10_000 + 30)


def test_future_prices_do_not_change_past_equity():
    base = {"A": [100, 101, 103, 102, 104, 106, 105], "B": [50, 49, 48, 50, 51, 49, 47]}
    p1 = make_panels(base)
    tampered = {k: v[:4] + [x * 3 for x in v[4:]] for k, v in base.items()}
    p2 = make_panels(tampered)
    sig1 = p1["close"].pct_change(2)
    sig2 = p2["close"].pct_change(2)
    member = p1["close"].notna()
    w1 = xs.quantile_weights(sig1, member, frac=0.5, min_names=1)
    w2 = xs.quantile_weights(sig2, member, frac=0.5, min_names=1)
    r1 = xs.run(w1, p1, costs=xs.Costs(funding=False))
    r2 = xs.run(w2, p2, costs=xs.Costs(funding=False))
    # 第 3 天（含）以前的权益只依赖第 4 天开盘及以前的价格
    pd.testing.assert_series_equal(r1.equity.iloc[:3], r2.equity.iloc[:3])
    assert r1.equity.iloc[-1] != pytest.approx(r2.equity.iloc[-1])


def test_delisted_symbol_is_closed_at_last_close():
    p = make_panels({"A": [100, 100, 90, 80, np.nan, np.nan], "B": [100] * 6})
    w = weights_on(p["open"].index, 0, {"A": 1.0, "B": 0.0})
    res = xs.run(w, p, costs=xs.Costs(fee=0.001, slip_other=0, funding=False))
    # 第 1 天开盘买入 10000（费 10），跌到 80 后在第 3 天收盘平仓（持仓 8000，费 8）
    assert res.daily["n_long"].iloc[3] == 0
    assert res.daily["fees"].iloc[3] == pytest.approx(8_000 * 0.001)
    assert res.equity.iloc[3] == pytest.approx(10_000 * 0.8 - 10 - 8.0)
    assert res.equity.iloc[-1] == pytest.approx(res.equity.iloc[3])  # 之后不再有敞口


def test_rebalance_only_on_schedule_and_slippage_by_rank():
    p = make_panels({"A": [100, 100, 100, 100, 100], "B": [100] * 5})
    idx = p["open"].index
    w = pd.DataFrame({"A": [1.0, 1.0, 0.5, 0.5, 0.5], "B": 0.0}, index=idx)
    when = pd.Series([True, False, False, True, False], index=idx)
    rank = pd.DataFrame({"A": 1.0, "B": 50.0}, index=idx)
    res = xs.run(xs.on_schedule(w, when), p, rank=rank, costs=xs.Costs(fee=0, slip_top=0.001, slip_other=0.01))
    assert res.daily["turnover"].tolist() == pytest.approx([0, 10_000, 0, 0, 5_000 - 0], rel=1e-3)
    assert res.daily["fees"].iloc[1] == pytest.approx(10.0)  # A 是前 10 名，用 slip_top


def test_quantile_weights_dollar_neutral():
    idx = pd.date_range(D0, periods=2)
    sig = pd.DataFrame([[1, 2, 3, 4, 5, 6, 7, 8, 9, 10]] * 2, index=idx, columns=list("abcdefghij"), dtype=float)
    member = sig.notna()
    member.loc[idx[1], "j"] = False
    w = xs.quantile_weights(sig, member, frac=0.2, min_names=2)
    assert w.loc[idx[0], ["i", "j"]].tolist() == [0.25, 0.25]
    assert w.loc[idx[0], ["a", "b"]].tolist() == [-0.25, -0.25]
    assert w.loc[idx[0]].sum() == pytest.approx(0) and w.loc[idx[0]].abs().sum() == pytest.approx(1)
    assert w.loc[idx[1], "j"] == 0 and w.loc[idx[1], "i"] > 0  # 非成员不入选
    assert (xs.quantile_weights(sig, member, frac=0.2, min_names=6) == 0).all().all()  # 品种不够就空仓


def test_walk_forward_only_uses_past():
    idx = pd.date_range("2020-01-01", periods=800)
    a = pd.Series(np.where(idx < pd.Timestamp("2021-01-01"), 0.01, -0.01), index=idx)
    b = pd.Series(np.where(idx < pd.Timestamp("2021-01-01"), -0.01, 0.01), index=idx)
    noise = pd.Series(np.random.default_rng(0).normal(0, 1e-4, len(idx)), index=idx)
    oos, chosen = xs.walk_forward({"a": a + noise, "b": b + noise}, train_days=365, test_days=90)
    assert chosen[0]["params"] == "a"           # 2021-01 之前只看得到 a 好
    assert oos.index[0] == pd.Timestamp("2020-12-31")
    assert chosen[-1]["params"] == "b"          # 训练窗口滚到 2021 之后改选 b
    assert {c["params"] for c in chosen} == {"a", "b"}


def test_stats_from_returns():
    r = pd.Series([0.1, -0.5, 0.2], index=pd.date_range(D0, periods=3))
    s = xs.stats_from_returns(r)
    assert s["total_return"] == pytest.approx(1.1 * 0.5 * 1.2 - 1)
    assert s["max_drawdown"] == pytest.approx(-0.5)


# ---------------------------------------------------------------- 全市场数据

def _zip(path, name: str, text: str):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.getvalue())


def test_load_symbol_handles_headerless_klines_and_daily_funding(tmp_path):
    ms = lambda d: int(pd.Timestamp(d).value // 1_000_000)  # noqa: E731
    _zip(tmp_path / "raw/klines/XUSDT/XUSDT-1d-2020-01.zip", "a.csv",
         f"{ms('2020-01-01')},1,2,0.5,1.5,10,0,100,5,0,0,0\n{ms('2020-01-02')},1.5,2,1,1.8,10,0,200,5,0,0,0\n")
    _zip(tmp_path / "raw/klines/XUSDT/XUSDT-1d-2020-02.zip", "b.csv",
         "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,"
         f"taker_buy_quote_volume,ignore\n{ms('2020-02-01')},2,3,1,2.5,10,0,300,5,0,0,0\n")
    _zip(tmp_path / "raw/funding/XUSDT/XUSDT-fundingRate-2020-01.zip", "f.csv",
         "calc_time,funding_interval_hours,last_funding_rate\n"
         f"{ms('2020-01-01')},8,0.0001\n{ms('2020-01-01 08:00')},8,0.0002\n{ms('2020-01-02')},8,-0.0003\n")
    _zip(tmp_path / "raw/premium/XUSDT/XUSDT-1d-2020-01.zip", "p.csv",
         f"{ms('2020-01-01')},0,0,0,-0.001,0,0,0,0,0,0,0\n")
    s = uv.load_symbol(tmp_path, "XUSDT")
    assert s["close"].tolist() == [1.5, 1.8, 2.5]
    assert s["close"].index[0] == pd.Timestamp("2020-01-01")
    assert s["quote_volume"].iloc[-1] == 300
    assert s["funding"].tolist() == pytest.approx([0.0003, -0.0003])
    assert s["premium"].iloc[0] == pytest.approx(-0.001)


def test_exclude_non_crypto_keeps_pre_tradfi_history():
    idx = pd.to_datetime(["2023-01-01", "2026-01-01"])
    df = pd.DataFrame({"BTCUSDT": [1.0, 2.0], "BTCDOMUSDT": [1.0, 1.0], "MDTUSDT": [3.0, 4.0]}, index=idx)
    out = uv.exclude_non_crypto({"close": df})["close"]
    assert "BTCDOMUSDT" not in out.columns
    assert out["MDTUSDT"].tolist()[0] == 3.0 and np.isnan(out["MDTUSDT"].iloc[1])  # 重名的 TradFi 只去掉之后


def test_point_in_time_universe_uses_only_past_volume():
    idx = pd.date_range("2022-11-01", "2023-05-31")
    close = pd.DataFrame(1.0, index=idx, columns=["OLD", "BIG", "NEW", "GONE"])
    close.loc[:"2023-02-25", "NEW"] = np.nan          # 2023-02-26 才上市
    close.loc["2023-03-15":, "GONE"] = np.nan          # 3 月中下架
    vol = pd.DataFrame({"OLD": 10.0, "BIG": 1.0, "NEW": 100.0, "GONE": 5.0}, index=idx)
    vol.loc["2023-04-01":, "BIG"] = 1e9                # 4 月起成交暴增：不能影响 4 月 1 日以前
    member, rank = uv.point_in_time_universe(close, vol, top_n=2, vol_window=30, min_age_days=60)
    assert member.loc["2023-03-10"].to_dict() == {"OLD": True, "BIG": False, "NEW": False, "GONE": True}
    assert member.loc["2023-04-10"].to_dict() == {"OLD": True, "BIG": True, "NEW": False, "GONE": False}
    # 上市满 60 天后才进来，挤掉成交额更小的 OLD
    assert member.loc["2023-05-10"].to_dict() == {"OLD": False, "BIG": True, "NEW": True, "GONE": False}
    assert rank.loc["2023-05-10", "BIG"] == 1 and rank.loc["2023-05-10", "NEW"] == 2
    assert not member.loc["2022-11-15"].any()          # 第一个月没有前一天的数据
    assert not member.loc["2022-12-15"].any()          # 上市不满 60 天
    assert member.loc["2023-01-15", "OLD"]
