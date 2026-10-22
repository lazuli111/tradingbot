"""向量化的多品种日线回测：输入目标权重面板，输出净值和成本拆分。横截面多空、多币种趋势共用。

时序约定（不看未来）：
- weights.loc[t] 是用第 t 天收盘及以前的数据算出的目标权重（占当时权益的比例，多正空负）；
  整行 NaN 表示这天不调仓。
- 在第 t+1 天开盘按开盘价调到目标：成交额 × (taker 费 + 滑点) 记为手续费。
- 持仓从开盘拿到下一天开盘；某品种下一天没有开盘价（下架或数据中断）时，按当天收盘价强制平仓并付成本。
- 资金费：当天持仓 × 当天资金费率合计，多头在正费率时付、空头收（负费率反过来）。

滑点按月初成交额排名分档：前 top_rank 名 slip_top，其余 slip_other。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from bot.metrics import compute_metrics

DAY_MS = 86_400_000


@dataclass
class Costs:
    fee: float = 0.0005          # Binance U 本位 taker 0.05%
    slip_top: float = 0.0002
    slip_other: float = 0.0005
    top_rank: int = 10
    funding: bool = True

    def scaled(self, k: float) -> "Costs":
        """成本敏感性：手续费和滑点乘 k（资金费不变）。"""
        return Costs(self.fee * k, self.slip_top * k, self.slip_other * k, self.top_rank, self.funding)


@dataclass
class XSResult:
    equity: pd.Series                 # 每天结束（下一天开盘）时的权益
    daily: pd.DataFrame               # pnl / fees / funding / turnover / gross / n_long / n_short
    initial: float
    metrics: dict = field(default_factory=dict)

    @property
    def returns(self) -> pd.Series:
        return self.equity / self.equity.shift(1, fill_value=self.initial) - 1


def run(weights: pd.DataFrame, panels: dict[str, pd.DataFrame], rank: pd.DataFrame | None = None,
        costs: Costs = Costs(), initial: float = 10_000.0, since=None, until=None) -> XSResult:
    """按目标权重回测。weights 的列是品种、索引是日期（可以只覆盖一部分品种 / 日期）。"""
    opens = panels["open"]
    idx = opens.index
    if since is not None:
        idx = idx[idx >= pd.Timestamp(since)]
    if until is not None:
        idx = idx[idx <= pd.Timestamp(until)]
    cols = [c for c in weights.columns if c in opens.columns]
    O = opens.reindex(index=idx, columns=cols).to_numpy(float)
    C = panels["close"].reindex(index=idx, columns=cols).to_numpy(float)
    F = (panels["funding"].reindex(index=idx, columns=cols).fillna(0.0).to_numpy(float)
         if costs.funding and "funding" in panels else np.zeros_like(O))
    # 第 i 天开盘执行的是第 i-1 天收盘算出的目标
    W = weights.reindex(columns=cols).reindex(idx.union(weights.index)).shift(1).reindex(idx).to_numpy(float)
    if rank is not None:
        R = rank.reindex(index=idx, columns=cols).to_numpy(float)
        slip = np.where(R <= costs.top_rank, costs.slip_top, costs.slip_other)
    else:
        slip = np.full_like(O, costs.slip_other)
    cost_rate = costs.fee + slip

    n, m = O.shape
    h = np.zeros(m)                    # 各品种持仓市值（多正空负）
    E = initial
    out = np.zeros((n, 8))
    for i in range(n):
        fees = turnover = 0.0
        has_open = ~np.isnan(O[i])
        h[~has_open] = 0.0             # 理论上前一天已经平掉，这里兜底
        w = W[i]
        if not np.all(np.isnan(w)):
            target = np.where(has_open, np.nan_to_num(w) * E, 0.0)
            trade = np.abs(target - h)
            fees = float((trade * cost_rate[i]).sum())
            turnover = float(trade.sum())
            h = target
            E -= fees
        # 开盘到下一天开盘；没有下一天开盘价的按今天收盘平仓
        if i + 1 < n:
            nxt = O[i + 1]
            exit_now = has_open & np.isnan(nxt)
        else:
            nxt = C[i]
            exit_now = np.zeros(m, bool)
        end_px = np.where(exit_now | (i + 1 >= n), C[i], nxt)
        r = np.where(has_open, end_px / O[i] - 1, 0.0)
        r = np.nan_to_num(r)
        fund = float((h * F[i]).sum())
        pnl = float((h * r).sum())
        h = h * (1 + r)
        if exit_now.any():
            closing = np.abs(h[exit_now])
            exit_fees = float((closing * cost_rate[i][exit_now]).sum())
            fees += exit_fees
            turnover += float(closing.sum())
            h[exit_now] = 0.0
            E -= exit_fees
        E += pnl - fund
        out[i] = (E, pnl, fees, fund, turnover, np.abs(h).sum() / E if E > 0 else np.nan,
                  (h > 0).sum(), (h < 0).sum())
        if E <= 0:
            out[i + 1:, 0] = 0.0
            break
    daily = pd.DataFrame(out, index=idx, columns=["equity", "pnl", "fees", "funding", "turnover", "gross",
                                                    "n_long", "n_short"])
    equity = daily.pop("equity")
    res = XSResult(equity, daily, initial)
    res.metrics = summarize(res, initial)
    return res


def summarize(res: XSResult, initial: float) -> dict:
    eq = res.equity
    eq_ms = pd.Series(eq.to_numpy(), index=eq.index.asi8 // 1_000_000)
    met = compute_metrics(eq_ms, [], initial, DAY_MS, fees=float(res.daily["fees"].sum()),
                          funding=float(res.daily["funding"].sum()))
    years = len(eq) / 365
    avg_eq = float(eq.mean()) if len(eq) else float("nan")
    met.update({
        "annual_turnover": float(res.daily["turnover"].sum()) / avg_eq / years if years and avg_eq else float("nan"),
        "avg_gross": float(res.daily["gross"].mean()),
        "fees_pct_per_year": float(res.daily["fees"].sum()) / avg_eq / years if years and avg_eq else float("nan"),
        "funding_pct_per_year": float(res.daily["funding"].sum()) / avg_eq / years if years and avg_eq else float("nan"),
    })
    for k in ("trades", "win_rate", "profit_factor", "avg_trade_pnl", "liquidations", "benchmark_return"):
        met.pop(k, None)
    return met


# ---------------------------------------------------------------- 策略共用的小工具

def schedule(index: pd.DatetimeIndex, freq: str) -> pd.Series:
    """哪些天收盘后做决定：'D' 每天；'W' 每周日（周一开盘执行）。"""
    if freq == "D":
        return pd.Series(True, index=index)
    if freq == "W":
        return pd.Series(index.dayofweek == 6, index=index)
    raise ValueError(f"未知调仓频率 {freq!r}")


def quantile_weights(signal: pd.DataFrame, member: pd.DataFrame, frac: float = 0.2,
                     gross: float = 1.0, min_names: int = 5) -> pd.DataFrame:
    """每天在成员里按 signal 排序：多最高的 frac、空最低的 frac，各占 gross/2，等权，美元中性。

    signal 缺失的品种不参与。某天可选品种不到 2 × min_names 时空仓。
    """
    sig = signal.where(member)
    count = sig.notna().sum(axis=1)
    k = np.floor(count * frac).clip(lower=0)
    pos_rank = sig.rank(axis=1, ascending=False, method="first")
    neg_rank = sig.rank(axis=1, ascending=True, method="first")
    longs = pos_rank.le(k, axis=0)
    shorts = neg_rank.le(k, axis=0)
    w = longs.astype(float).div(k.replace(0, np.nan), axis=0) * (gross / 2) \
        - shorts.astype(float).div(k.replace(0, np.nan), axis=0) * (gross / 2)
    return w.where(count >= 2 * min_names, 0.0).fillna(0.0)


def on_schedule(weights: pd.DataFrame, when: pd.Series) -> pd.DataFrame:
    """只在 when 为 True 的日子给出目标，其余日子整行 NaN（不调仓）。"""
    keep = when.reindex(weights.index, fill_value=False).to_numpy(bool)
    out = weights.copy()
    out.loc[~keep] = np.nan
    return out


def walk_forward(returns: dict, train_days: int = 365, test_days: int = 90, start=None,
                 objective: str = "sharpe") -> tuple[pd.Series, list[dict]]:
    """在若干组参数的日收益序列里滚动选参：每个测试窗口只用之前 train_days 天挑最优的一组。

    returns: {参数键: 日收益 Series}（各自完整跑一遍得到的）。切换参数时的换仓成本没有计入，
    报告里要注明。返回 (拼接起来的样本外日收益, 每个窗口选了哪组)。
    """
    df = pd.DataFrame(returns).dropna(how="all").fillna(0.0)
    idx = df.index
    t = pd.Timestamp(start) if start is not None else idx[0] + pd.Timedelta(days=train_days)
    pieces, chosen = [], []
    while t <= idx[-1]:
        train = df[(idx >= t - pd.Timedelta(days=train_days)) & (idx < t)]
        test = df[(idx >= t) & (idx < t + pd.Timedelta(days=test_days))]
        if len(train) < train_days // 2 or test.empty:
            t += pd.Timedelta(days=test_days)
            continue
        if objective == "sharpe":
            score = train.mean() / train.std().replace(0, np.nan)
        else:
            score = (1 + train).prod()
        best = score.idxmax() if score.notna().any() else df.columns[0]
        pieces.append(test[best])
        chosen.append({"start": t.date(), "params": best, "train_score": float(score.get(best, np.nan))})
        t += pd.Timedelta(days=test_days)
    oos = pd.concat(pieces) if pieces else pd.Series(dtype=float)
    return oos, chosen


def stats_from_returns(r: pd.Series) -> dict:
    """日收益序列的年化收益、夏普、最大回撤（walk-forward 样本外用）。"""
    r = r.dropna()
    if r.empty:
        return {"cagr": float("nan"), "sharpe": float("nan"), "max_drawdown": float("nan"), "days": 0}
    eq = (1 + r).cumprod()
    years = len(r) / 365
    return {"total_return": float(eq.iloc[-1] - 1),
            "cagr": float(eq.iloc[-1] ** (1 / years) - 1) if eq.iloc[-1] > 0 else float("nan"),
            "sharpe": float(r.mean() / r.std() * np.sqrt(365)) if r.std() > 0 else float("nan"),
            "max_drawdown": float((eq / eq.cummax() - 1).min()), "days": len(r)}
