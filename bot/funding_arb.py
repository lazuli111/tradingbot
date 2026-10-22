"""Funding-rate carry (long spot + short perpetual, delta-neutral): backtest helpers.

Accounting, per 1 USD of target notional on each leg:
  - both legs hold the same coin quantity q, so price moves cancel and only the
    basis (perp - spot) moves the P&L: d_pnl = -q * d(perp - spot)
  - the short perp receives q * perp * rate at every funding event (rate > 0 means
    longs pay shorts, which is income here; rate < 0 is a holding cost)
  - opening or closing pays spot fee + perp fee (+ slippage on each leg) on the traded notional
  - q is reset to 1 / spot whenever the notional drifts more than `rebalance_band`
    from 1, paying fees on the traded difference. Without that the short leg's margin
    would run out on a rally (see `risk_metrics`), and returns would scale with price.

Positions are decided at funding events using only rates already settled.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

HOUR_MS = 3600 * 1000
DAY_MS = 24 * HOUR_MS
YEAR_HOURS = 365 * 24


@dataclass(frozen=True)
class Costs:
    spot_fee: float = 0.001
    perp_fee: float = 0.0005
    slippage: float = 0.0
    rebalance_band: float = 0.1


@dataclass(frozen=True)
class Grid:
    lookbacks: tuple[int, ...] = (3, 9, 21, 42, 90)
    thresholds: tuple[float, ...] = (0.0, 0.05, 0.10, 0.15, 0.20, 0.30)  # annualized


def fmt_ts(ts) -> str:
    return pd.Timestamp(int(ts), unit="ms", tz="UTC").strftime("%Y-%m-%d")


def align_hourly(funding: pd.DataFrame, spot: pd.DataFrame, perp: pd.DataFrame) -> pd.DataFrame:
    """One row per hour both markets traded. Prices are the bar's open, i.e. the price at
    `timestamp`; `rate` is set on the hour a funding payment settled and NaN elsewhere."""
    px = pd.merge(spot[["timestamp", "open", "high", "low"]].rename(
                      columns={"open": "spot", "high": "spot_high", "low": "spot_low"}),
                  perp[["timestamp", "open", "high", "low"]].rename(
                      columns={"open": "perp", "high": "perp_high", "low": "perp_low"}),
                  on="timestamp", how="inner")
    f = funding.assign(timestamp=(funding["timestamp"] // HOUR_MS) * HOUR_MS)
    f = f.groupby("timestamp", as_index=False)["rate"].sum()
    out = px.merge(f, on="timestamp", how="left").sort_values("timestamp").reset_index(drop=True)
    out["basis"] = out["perp"] / out["spot"] - 1
    return out


def funding_events(hourly: pd.DataFrame) -> pd.DataFrame:
    ev = hourly.dropna(subset=["rate"]).reset_index(drop=True)
    ev = ev[["timestamp", "rate", "spot", "perp", "basis"]].copy()
    gap = ev["timestamp"].diff() / HOUR_MS
    # Binance moves some symbols between 8h / 4h / 1h intervals; annualize each rate by its own interval
    ev["interval_h"] = gap.fillna(gap.iloc[1] if len(gap) > 1 else 8.0)
    ev["apr"] = ev["rate"] * YEAR_HOURS / ev["interval_h"]
    return ev


def span_years(events: pd.DataFrame) -> float:
    if len(events) < 2:
        return 0.0
    return (int(events["timestamp"].iloc[-1]) - int(events["timestamp"].iloc[0])) / (YEAR_HOURS * HOUR_MS)


def always_hold(events: pd.DataFrame) -> np.ndarray:
    return np.ones(len(events), dtype=bool)


def threshold_hold(events: pd.DataFrame, lookback: int, threshold: float) -> np.ndarray:
    """Hold after event i when the mean annualized rate of events i-lookback+1..i exceeds threshold."""
    avg = events["apr"].rolling(lookback, min_periods=lookback).mean()
    return (avg > threshold).to_numpy()


def simulate(events: pd.DataFrame, hold: np.ndarray, costs: Costs) -> dict:
    """hold[i] = position held after event i (through the next funding). Closed at the last event."""
    spot = events["spot"].to_numpy(float)
    perp = events["perp"].to_numpy(float)
    rate = events["rate"].to_numpy(float)
    n = len(events)
    hold = np.asarray(hold, dtype=bool).copy()
    if n:
        hold[-1] = False
    spot_cost = costs.spot_fee + costs.slippage
    perp_cost = costs.perp_fee + costs.slippage

    q = 0.0
    fund_pos = fund_neg = basis = fees = rebal_fees = 0.0
    trades = held = 0
    equity = np.zeros(n)
    for i in range(n):
        if q > 0:
            pay = q * perp[i] * rate[i]
            if pay >= 0:
                fund_pos += pay
            else:
                fund_neg += pay
            basis -= q * ((perp[i] - spot[i]) - (perp[i - 1] - spot[i - 1]))
            held += 1
        target = 1.0 / spot[i] if hold[i] else 0.0
        if q == 0 and target > 0:
            fees += target * (spot[i] * spot_cost + perp[i] * perp_cost)
            trades += 1
            q = target
        elif q > 0 and target == 0:
            fees += q * (spot[i] * spot_cost + perp[i] * perp_cost)
            q = 0.0
        elif q > 0 and abs(q * spot[i] - 1) > costs.rebalance_band:
            rebal_fees += abs(target - q) * (spot[i] * spot_cost + perp[i] * perp_cost)
            q = target
        equity[i] = fund_pos + fund_neg + basis - fees - rebal_fees
    return {"funding_pos": fund_pos, "funding_neg": fund_neg, "basis": basis, "fees": fees,
            "rebalance_fees": rebal_fees, "net": float(equity[-1]) if n else 0.0, "trades": trades,
            "periods_held": held, "periods": max(n - 1, 0), "equity": equity,
            "timestamp": events["timestamp"].to_numpy()}


def max_drawdown_abs(equity: np.ndarray) -> float:
    """Largest peak-to-trough fall of a cumulative P&L curve (in notional units, <= 0)."""
    if len(equity) == 0:
        return 0.0
    curve = np.r_[0.0, equity]
    return float((curve - np.maximum.accumulate(curve)).min())


def summarize(res: dict, years: float, prefix: str = "") -> dict:
    y = years if years > 0 else float("nan")
    out = {
        "funding_apr": (res["funding_pos"] + res["funding_neg"]) / y,
        "funding_pos_apr": res["funding_pos"] / y,
        "funding_neg_apr": res["funding_neg"] / y,
        "basis_apr": res["basis"] / y,
        "fee_apr": (res["fees"] + res["rebalance_fees"]) / y,
        "rebalance_fee_apr": res["rebalance_fees"] / y,
        "net_apr": res["net"] / y,
        "net_total": res["net"],
        "max_dd": max_drawdown_abs(res["equity"]),
        "trades": res["trades"],
        "time_in_market": res["periods_held"] / res["periods"] if res["periods"] else 0.0,
    }
    return {prefix + k: v for k, v in out.items()}


def walk_forward(events: pd.DataFrame, costs: Costs, grid: Grid,
                 train_days: int = 180, test_days: int = 90) -> dict:
    """Rolling train -> test. Each test window uses the (lookback, threshold) with the best net
    P&L on the preceding train window; the out-of-sample position path is then simulated in one
    pass, so a position carried across a window boundary isn't charged a round trip."""
    ts = events["timestamp"].to_numpy()
    paths = {(lb, th): threshold_hold(events, lb, th) for lb in grid.lookbacks for th in grid.thresholds}
    start = int(ts[0]) + train_days * DAY_MS
    oos_hold = np.zeros(len(events), dtype=bool)
    windows = []
    a = start
    while a < ts[-1]:
        b = a + test_days * DAY_MS
        train = (ts >= a - train_days * DAY_MS) & (ts < a)
        test = (ts >= a) & (ts < b)
        best, best_net = None, -np.inf
        for key, path in paths.items():
            net = simulate(events[train], path[train], costs)["net"]
            if net > best_net:
                best, best_net = key, net
        oos_hold[test] = paths[best][test]
        windows.append({"test_start": fmt_ts(a), "lookback": best[0], "threshold_apr": best[1],
                        "train_net": best_net, "test_time_in_market": float(paths[best][test].mean())
                        if test.any() else 0.0})
        a = b
    mask = ts >= start
    oos_events = events[mask].reset_index(drop=True)
    return {"events": oos_events, "result": simulate(oos_events, oos_hold[mask], costs), "windows": windows}


def _forward_max(values: np.ndarray, window: int) -> np.ndarray:
    """max(values[k : k + window]) for every k (shorter at the tail)."""
    s = pd.Series(values[::-1])
    return s.rolling(window, min_periods=1).max().to_numpy()[::-1]


def risk_metrics(hourly: pd.DataFrame, leverages=(1, 2, 3), mmr: float = 0.004) -> dict:
    """Basis widening and short-leg liquidation exposure from hourly bars.

    - max_basis_widening: largest rise of (perp / spot - 1) from any earlier hour; that rise is
      the mark-to-market loss of the hedge per unit notional.
    - runup_<H>: largest rise of the perp's high over H hours from a bar's open. An isolated
      short at leverage L is liquidated after a rise of 1/L - mmr unless margin is topped up or
      the hedge rebalanced in between.
    - liq_<L>x_<H>: how many back-to-back H-hour blocks would have liquidated an L-x short
      whose margin is only reset at the start of each block.
    """
    basis = hourly["basis"].to_numpy(float)
    out = {"basis_mean": float(np.mean(basis)), "basis_min": float(basis.min()),
           "basis_max": float(basis.max()),
           "max_basis_widening": float((basis - np.minimum.accumulate(basis)).max())}
    opens = hourly["perp"].to_numpy(float)
    highs = hourly["perp_high"].to_numpy(float)
    for label, hours in (("8h", 8), ("1d", 24), ("7d", 24 * 7), ("30d", 24 * 30)):
        runup = _forward_max(highs, hours) / opens - 1
        out[f"runup_{label}"] = float(runup.max())
        blocks = runup[::hours]
        for lev in leverages:
            out[f"liq_{lev}x_{label}"] = int((blocks >= 1 / lev - mmr).sum())
    for lev in leverages:
        out[f"liq_distance_{lev}x"] = 1 / lev - mmr
    return out


def yearly_funding_apr(events: pd.DataFrame) -> dict:
    """Mean annualized funding rate per calendar year (what an always-on short collects, before costs)."""
    year = pd.to_datetime(events["timestamp"], unit="ms", utc=True).dt.year
    return {int(y): float(v) for y, v in events["apr"].groupby(year).mean().items()}


def format_table(table: pd.DataFrame, risk: pd.DataFrame, leverage: float = 1.0,
                 yearly: pd.DataFrame | None = None) -> str:
    cap = 1 + 1 / leverage
    lines = [f"逐品种（年化，按每条腿 1 单位名义；“按资本”= 名义收益 ÷ {cap:.2f}，即现货 + {leverage:g}x 保证金）", "",
             "| 品种 | 区间 | 正费率收入 | 负费率成本 | 基差 | 手续费 | 净（名义） | 净（按资本） | 最大回撤 "
             "| 样本外起点 | 样本外 一直持有 | 样本外 walk-forward | WF 持仓时间 / 开仓次数 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in table.itertuples():
        lines.append(
            f"| {r.symbol} | {r.start}~{r.end} | {r.always_funding_pos_apr:+.2%} | {r.always_funding_neg_apr:+.2%} "
            f"| {r.always_basis_apr:+.2%} | {-r.always_fee_apr:+.2%} | {r.always_net_apr:+.2%} "
            f"| {r.always_net_apr / cap:+.2%} | {r.always_max_dd:.2%} | {r.oos_start} "
            f"| {r.oos_always_net_apr:+.2%} | {r.oos_wf_net_apr:+.2%} "
            f"| {r.oos_wf_time_in_market:.0%} / {r.oos_wf_trades} |")
    lines += ["", "风险（小时线）", "",
              "| 品种 | 基差均值 / 区间 | 最大基差扩大 | 8h / 1d / 7d / 30d 最大涨幅 "
              "| 强平次数 1x / 2x / 3x（每 8h 补保证金） | 1x / 2x / 3x（每 7 天补） |",
              "| --- | --- | --- | --- | --- | --- |"]
    for r in risk.itertuples():
        lines.append(
            f"| {r.symbol} | {r.basis_mean:+.3%} / [{r.basis_min:+.2%}, {r.basis_max:+.2%}] "
            f"| {r.max_basis_widening:.2%} | {r.runup_8h:.0%} / {r.runup_1d:.0%} / {r.runup_7d:.0%} / "
            f"{r.runup_30d:.0%} | {r.liq_1x_8h} / {r.liq_2x_8h} / {r.liq_3x_8h} "
            f"| {r.liq_1x_7d} / {r.liq_2x_7d} / {r.liq_3x_7d} |")
    if yearly is not None:
        years = [c for c in yearly.columns if c != "symbol"]
        lines += ["", "逐年平均资金费（年化，未扣成本）", "",
                  "| 品种 | " + " | ".join(str(y) for y in years) + " |",
                  "| --- |" + " --- |" * len(years)]
        for r in yearly.itertuples(index=False):
            vals = [f"{v:+.2%}" if pd.notna(v) else "" for v in r[1:]]
            lines.append(f"| {r[0]} | " + " | ".join(vals) + " |")
    return "\n".join(lines)
