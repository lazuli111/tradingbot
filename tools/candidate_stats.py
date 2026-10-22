"""候选品种体检：数据从哪天开始、有多长、和基准（默认 BTC）的日收益相关性。

    python tools/candidate_stats.py --symbols XAU/USDT,PAXG/USDT,SPY/USDT --offline

除了价格的相关性，还会把策略单独跑一遍，给出策略日收益和基准策略的相关性——
组合层真正分散的是“策略的盈亏”，标的价格不相关不代表趋势策略的盈亏也不相关。
数据不到 --min-years（默认 1.5 年）的品种会标出来。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.engine.backtest import run_backtest  # noqa: E402
from bot.engine.portfolio import DAY_MS, daily_close  # noqa: E402
from portfolio_backtest import load_symbol  # noqa: E402


def daily_returns(df: pd.DataFrame) -> pd.Series:
    return np.log(daily_close(df)).diff().dropna()


def main() -> int:
    ap = argparse.ArgumentParser(description="候选品种体检")
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--benchmark", default="BTC/USDT")
    ap.add_argument("--config", default="config.example.yaml")
    ap.add_argument("--exchange", default="binance")
    ap.add_argument("--market", default="perp", choices=["spot", "perp"])
    ap.add_argument("--timeframe", default="4h")
    ap.add_argument("--since", default="2020-09-01")
    ap.add_argument("--until", default=None)
    ap.add_argument("--min-years", type=float, default=1.5)
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()

    def strategy_returns(cfg, df, funding):
        res = run_backtest(cfg, df, funding)
        return daily_returns(res.equity.rename("close").reset_index()), res.metrics

    _, bcfg, bdf, bfund = load_symbol(args.benchmark, args, args.offline)
    bench_px = daily_returns(bdf)
    bench_st, _ = strategy_returns(bcfg, bdf, bfund)

    print(f"{'品种':<16}{'首根K线':>12}{'天数':>6}{'年化波动':>9}{'价格相关':>9}{'策略相关':>9}"
          f"{'策略收益':>10}{'夏普':>7}{'交易':>6}  备注")
    for spec in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        name, cfg, df, funding = load_symbol(spec, args, args.offline)
        first = int(df["timestamp"].iloc[0])
        days = (int(df["timestamp"].iloc[-1]) - first) / DAY_MS
        px = daily_returns(df)
        corr_px = px.corr(bench_px.reindex(px.index))
        try:
            st, m = strategy_returns(cfg, df, funding)
            corr_st = st.corr(bench_st.reindex(st.index))
            perf = f"{m['total_return']:>+10.1%}{m['sharpe']:>7.2f}{m['trades']:>6}"
        except ValueError:  # too short to get past the indicator warm-up
            corr_st, perf = float("nan"), f"{'—':>10}{'—':>7}{'—':>6}"
        note = f"不足 {args.min_years:g} 年" if days < args.min_years * 365 else ""
        print(f"{name:<16}{pd.Timestamp(first, unit='ms'):%Y-%m-%d}{days:>6.0f}"
              f"{px.std() * np.sqrt(365):>9.0%}{corr_px:>9.2f}{corr_st:>9.2f}{perf}  {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
