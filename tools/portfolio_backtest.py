"""组合回测：N 个品种共用一份本金，三种分配方式。

    python tools/portfolio_backtest.py --symbols BTC/USDT,ETH/USDT,SOL/USDT --since 2023-01-01
    python tools/portfolio_backtest.py --symbols BTC/USDT,SOL/USDT --weighting invvol --vol-lookback 60

--weighting
  split   （默认，旧口径）每个品种一份 1/N 的独立子账户，最后把权益曲线加起来；子账户之间不再平衡。
  equal   共享账户：每次开仓按“账户总权益 × 1/N”算仓位，和线上 equity_fraction=0.5 的跑法一致。
  invvol  共享账户：每天按过去 N 天日收益波动率的倒数分配权重（只用前一天及以前的数据），
          权重只影响下一次开仓的仓位，已有持仓不调整，所以不额外产生订单。

品种写成 `PAXG/USDT@spot` 表示：价格用现货 K 线（上市更早），按永续规则回测、不计资金费。
这只是代理数据，用来把短历史品种延伸到长窗口，读结果时要单独看待。

每个币仍按自己的配置（含回撤熔断）独立运行；split 模式下熔断只冻结自己那 1/N。
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.config import load_config  # noqa: E402
from bot.data import load_funding, load_ohlcv, make_exchange, timeframe_ms  # noqa: E402
from bot.engine.backtest import run_backtest  # noqa: E402
from bot.engine.portfolio import (daily_close, equal_weights, inverse_vol_weights,  # noqa: E402
                                  run_portfolio, turnover)
from bot.metrics import compute_metrics  # noqa: E402
from bot.report import format_metrics  # noqa: E402


class CacheOnly:
    """--offline：不联网，只用 data/ 里已有的缓存（例如在别的机器上拉好的 Binance 数据）。"""

    def fetch_ohlcv(self, *args, **kwargs):
        return []

    def fetch_funding_rate_history(self, *args, **kwargs):
        return []


def load_symbol(spec: str, args, offline: bool):
    """-> (name, cfg, ohlcv, funding). `spec` 可以带 @spot（见模块说明）。"""
    sym, _, price_market = spec.partition("@")
    cfg = load_config(args.config, {"exchange": args.exchange, "market": args.market,
                                    "symbol": sym, "timeframe": args.timeframe})
    data_cfg = replace(cfg, market=price_market) if price_market else cfg
    ex = CacheOnly() if offline else make_exchange(data_cfg.exchange, data_cfg.market)
    df = load_ohlcv(data_cfg, args.since, args.until, ex)
    funding = load_funding(cfg, args.since, args.until, ex) if cfg.is_perp and not price_market else None
    return spec, cfg, df, funding


def run_split(loaded, args):
    """旧口径：N 份独立子账户，曲线相加。"""
    weight = 1.0 / len(loaded)
    equity_parts, trades, rows = {}, [], []
    total_capital = loaded[0][1].initial_equity
    for name, cfg, df, funding in loaded:
        cfg = replace(cfg, initial_equity=cfg.initial_equity * weight)  # 每个币一份等额本金
        res = run_backtest(cfg, df, funding)
        equity_parts[name] = res.equity
        trades += res.trades
        rows.append((name, res.metrics))
    port_equity = pd.concat(equity_parts.values(), axis=1).ffill().dropna().sum(axis=1)
    bar_ms = timeframe_ms(args.timeframe)
    metrics = compute_metrics(port_equity, trades, total_capital, bar_ms,
                              fees=sum(m["fees"] for _, m in rows), funding=sum(m["funding"] for _, m in rows))
    metrics["turnover"] = turnover(trades, port_equity, bar_ms)

    print(f"{'币种':<14}{'收益':>10}{'夏普':>8}{'回撤':>10}{'交易':>6}{'资金费':>10}  熔断")
    for name, m in rows:
        print(f"{name.split('/')[0]:<14}{m['total_return']:>+10.2%}{m['sharpe']:>8.2f}"
              f"{m['max_drawdown']:>+10.2%}{m['trades']:>6}{m['funding']:>10.0f}"
              f"  {'是（之后空仓）' if m['max_drawdown'] <= -0.1999 else ''}")
    print(f"\n分散效果：组合波动 {port_equity.pct_change().std():.4%}/根，"
          f"最差单币回撤 {min(m['max_drawdown'] for _, m in rows):.1%}，组合回撤 {metrics['max_drawdown']:.1%}")
    return port_equity, metrics, f"{len(loaded)} 个品种 × {weight:.0%}（独立子账户）"


def strategy_prices(loaded) -> pd.DataFrame:
    """每个品种单独跑一遍策略的权益曲线（日频），给 --vol-source strategy 用。"""
    return pd.concat({name: daily_close(run_backtest(cfg, df, funding).equity.rename("close")
                                        .reset_index()) for name, cfg, df, funding in loaded}, axis=1)


def run_shared(loaded, args):
    capital = loaded[0][1].initial_equity
    if args.weighting == "invvol" and args.vol_source == "strategy":
        prices = strategy_prices(loaded)
    else:
        prices = pd.concat({name: daily_close(df) for name, _, df, _ in loaded}, axis=1)
    weights = (inverse_vol_weights(prices, args.vol_lookback) if args.weighting == "invvol"
               else equal_weights(prices))
    res = run_portfolio({name: cfg for name, cfg, _, _ in loaded},
                        {name: (df, funding) for name, _, df, funding in loaded}, weights, capital)

    used = weights.loc[weights.index >= res.equity.index[0] // 86_400_000 * 86_400_000]
    print(f"{'品种':<14}{'盈亏':>12}{'交易':>6}{'资金费':>10}{'平均权重':>10}{'最小':>8}{'最大':>8}")
    for name, s in res.per_symbol.items():
        w = used[name]
        print(f"{name.split('/')[0]:<14}{s['pnl']:>+12.0f}{s['trades']:>6}{s['funding']:>10.0f}"
              f"{w.mean():>10.1%}{w.min():>8.1%}{w.max():>8.1%}")
    label = ("等权（共享账户）" if args.weighting == "equal"
             else f"波动率倒数 N={args.vol_lookback} 天（{args.vol_source}）")
    return res.equity, res.metrics, f"{len(loaded)} 个品种 · {label}"


def main() -> int:
    ap = argparse.ArgumentParser(description="组合回测")
    ap.add_argument("--symbols", default="BTC/USDT,ETH/USDT,SOL/USDT")
    ap.add_argument("--config", default="config.example.yaml")
    ap.add_argument("--exchange", default="binance")
    ap.add_argument("--market", default="perp", choices=["spot", "perp"])
    ap.add_argument("--timeframe", default="4h")
    ap.add_argument("--since", default="2023-01-01")
    ap.add_argument("--until", default=None)
    ap.add_argument("--weighting", default="split", choices=["split", "equal", "invvol"])
    ap.add_argument("--vol-lookback", type=int, default=60, help="invvol 的回看天数")
    ap.add_argument("--vol-source", default="asset", choices=["asset", "strategy"],
                    help="invvol 用标的价格的波动率，还是该品种策略权益曲线的波动率")
    ap.add_argument("--offline", action="store_true", help="只用 data/ 里的缓存，不联网")
    args = ap.parse_args()

    loaded = [load_symbol(s.strip(), args, args.offline) for s in args.symbols.split(",") if s.strip()]
    port_equity, metrics, label = (run_split if args.weighting == "split" else run_shared)(loaded, args)

    start = port_equity.index[0]
    closes = pd.concat([df.set_index("timestamp")["close"] for _, _, df, _ in loaded], axis=1).ffill()
    closes = closes.loc[closes.index >= start].dropna()
    metrics["benchmark_return"] = float((closes.iloc[-1] / closes.iloc[0]).mean() - 1)
    print()
    print(format_metrics(metrics, f"组合 | {args.exchange} {args.market} {args.timeframe} | {label} | "
                                  f"{len(port_equity)} 根 K 线"))
    print(f"{'等权买入持有':<16}{metrics['benchmark_return']:>+20.2%}")
    print(f"{'年换手（倍）':<16}{metrics['turnover']:>20.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
