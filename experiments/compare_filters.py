"""Do the entry filters help? Compare fixed, textbook filter settings against the
unfiltered strategy across several symbols and two separate halves of history.

Thresholds are fixed up front (not tuned on this data). A filter is only worth
keeping if it improves most symbol x period x strategy cells, not just the total.

    .venv/bin/python experiments/compare_filters.py --exchange okx --since 2023-01-01
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.config import StrategyConfig, load_config  # noqa: E402
from bot.data import load_funding, load_ohlcv, make_exchange, to_ms  # noqa: E402
from bot.engine.backtest import run_backtest  # noqa: E402
from bot.engine.optimize import _quiet  # noqa: E402

BASE = {"ma_cross": {"fast": 20, "slow": 50}, "donchian": {"entry": 20, "exit": 10}}
VARIANTS = {
    "无过滤": {},
    "ADX≥20": {"adx_min": 20},
    "ADX≥25": {"adx_min": 25},
    "波动率≤80分位": {"vol_max_pct": 0.8},
    "波动率≥20分位": {"vol_min_pct": 0.2},
    "ADX≥20+波动率≤80": {"adx_min": 20, "vol_max_pct": 0.8},
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exchange", default="okx")
    ap.add_argument("--market", default="perp", choices=["spot", "perp"])
    ap.add_argument("--symbols", default="BTC/USDT,ETH/USDT,SOL/USDT")
    ap.add_argument("--timeframe", default="4h")
    ap.add_argument("--since", default="2023-01-01")
    ap.add_argument("--strategies", default="ma_cross,donchian")
    args = ap.parse_args()
    logging.basicConfig(level=logging.ERROR)

    rows = []
    for symbol in args.symbols.split(","):
        cfg = load_config("config.example.yaml", {"exchange": args.exchange, "market": args.market,
                                                  "symbol": symbol, "timeframe": args.timeframe})
        # compare signal quality, not who trips the (permanent) drawdown breaker first;
        # stops and position sizing stay as configured
        cfg = replace(cfg, risk=replace(cfg.risk, max_drawdown=0.99, daily_loss_limit=0.99))
        ex = make_exchange(cfg.exchange, cfg.market)
        df = load_ohlcv(cfg, args.since, ex=ex)
        funding = load_funding(cfg, args.since, ex=ex) if cfg.is_perp else None
        ts = df["timestamp"].to_numpy()
        mid = int(ts[0] + (ts[-1] - ts[0]) // 2)
        halves = {"前半": (df[ts < mid], None), "后半": (df, mid)}
        print(f"{symbol}: {len(df)} 根 K 线, 前后半分界 {pd.Timestamp(mid, unit='ms', tz='UTC').date()}",
              file=sys.stderr)
        for strat in args.strategies.split(","):
            for variant, extra in VARIANTS.items():
                c = replace(cfg, strategy=StrategyConfig(strat, {**BASE[strat], **extra}))
                for half, (data, start) in halves.items():
                    with _quiet():
                        m = run_backtest(c, data, funding, start_ts=start).metrics
                    rows.append({"symbol": symbol.split("/")[0], "strategy": strat, "variant": variant,
                                 "half": half, "return": m["total_return"], "sharpe": m["sharpe"],
                                 "max_dd": m["max_drawdown"], "trades": m["trades"],
                                 "bench": m["benchmark_return"]})

    res = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    for strat in args.strategies.split(","):
        sub = res[res.strategy == strat]
        print(f"\n=== {strat} {BASE[strat]} | {args.market} {args.timeframe} | 夏普（收益）===")
        table = sub.assign(cell=sub.apply(lambda r: f"{r.sharpe:5.2f} ({r['return']:+.0%})", axis=1)) \
                   .pivot(index="variant", columns=["symbol", "half"], values="cell") \
                   .reindex(list(VARIANTS))
        print(table.to_string())
        bench = sub[sub.variant == "无过滤"].set_index(["symbol", "half"])["bench"]
        print("买入持有: " + "  ".join(f"{s}-{h} {v:+.0%}" for (s, h), v in bench.items()))

    base = res[res.variant == "无过滤"].set_index(["symbol", "strategy", "half"])
    print("\n=== 相对无过滤的改善（所有 币种×策略×半段 单元格）===")
    print(f"{'过滤器':<18}{'夏普变好的格子':>14}{'平均夏普变化':>12}{'回撤变化(+更小)':>14}{'平均交易数':>10}")
    for variant in list(VARIANTS)[1:]:
        v = res[res.variant == variant].set_index(["symbol", "strategy", "half"])
        d_sharpe = (v["sharpe"] - base["sharpe"]).dropna()
        d_dd = v["max_dd"] - base["max_dd"]
        print(f"{variant:<18}{(d_sharpe > 0).sum():>8} / {len(d_sharpe):<5}{d_sharpe.mean():>+12.2f}"
              f"{d_dd.mean():>+14.1%}{v['trades'].mean():>10.0f}")
    print(f"{'(无过滤)':<18}{'':>14}{'':>12}{'':>12}{base['trades'].mean():>10.0f}")


if __name__ == "__main__":
    main()
