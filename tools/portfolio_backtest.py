"""等权组合回测：N 个币共用一份本金，每个币拿 1/N，最后把权益曲线加起来看。

    python tools/portfolio_backtest.py --symbols BTC/USDT,ETH/USDT,SOL/USDT --since 2023-01-01

和"单币各自跑一遍"的区别：这是一份钱分给 N 个币，所以组合收益/回撤是可信的；
每个币仍按自己的配置（含 20% 回撤熔断）独立运行，熔断只冻结自己那 1/N。
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
from bot.metrics import compute_metrics  # noqa: E402
from bot.report import format_metrics  # noqa: E402


def combine(equity_parts: dict, close_parts: dict, total_capital: float) -> tuple[pd.Series, pd.Series]:
    """各币权益相加成组合权益；收盘价归一后取均值，作为等权买入持有基准。

    整数时间戳索引 concat 后不会排序：上市晚的币排在前面时，排在后面的老币独有的早期 K 线
    会被追加到末尾，ffill 后末尾几行全是错位的数，所以先 sort_index 再 ffill。
    """
    port = pd.concat(equity_parts.values(), axis=1).sort_index().ffill().dropna()
    basket = pd.concat(close_parts.values(), axis=1).sort_index().ffill()
    return port.sum(axis=1), (basket / basket.iloc[0]).mean(axis=1) * total_capital


def main() -> int:
    ap = argparse.ArgumentParser(description="等权组合回测")
    ap.add_argument("--symbols", default="BTC/USDT,ETH/USDT,SOL/USDT")
    ap.add_argument("--config", default="config.example.yaml")
    ap.add_argument("--exchange", default="binance")
    ap.add_argument("--market", default="perp", choices=["spot", "perp"])
    ap.add_argument("--timeframe", default="4h")
    ap.add_argument("--since", default="2023-01-01")
    ap.add_argument("--until", default=None)
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    weight = 1.0 / len(symbols)
    equity_parts, close_parts, bench_parts, trades, rows = {}, {}, {}, [], []
    total_capital = None

    for sym in symbols:
        cfg = load_config(args.config, {"exchange": args.exchange, "market": args.market,
                                        "symbol": sym, "timeframe": args.timeframe})
        total_capital = total_capital or cfg.initial_equity
        cfg = replace(cfg, initial_equity=cfg.initial_equity * weight)  # 每个币一份等额本金
        ex = make_exchange(cfg.exchange, cfg.market)
        df = load_ohlcv(cfg, args.since, args.until, ex)
        funding = load_funding(cfg, args.since, args.until, ex) if cfg.is_perp else None
        res = run_backtest(cfg, df, funding)
        equity_parts[sym] = res.equity
        close_parts[sym] = df.set_index("timestamp")["close"].reindex(res.equity.index).ffill()
        trades += res.trades
        rows.append((sym, res.metrics))

    port_equity, basket = combine(equity_parts, close_parts, total_capital)

    print(f"{'币种':<10}{'收益':>10}{'夏普':>8}{'回撤':>10}{'交易':>6}{'资金费':>10}  熔断")
    for sym, m in rows:
        print(f"{sym.split('/')[0]:<10}{m['total_return']:>+10.2%}{m['sharpe']:>8.2f}"
              f"{m['max_drawdown']:>+10.2%}{m['trades']:>6}{m['funding']:>10.0f}"
              f"  {'是（之后空仓）' if m['max_drawdown'] <= -0.1999 else ''}")

    metrics = compute_metrics(port_equity, trades, total_capital, timeframe_ms(args.timeframe),
                              benchmark_return=float(basket.iloc[-1] / total_capital - 1),
                              fees=sum(m["fees"] for _, m in rows), funding=sum(m["funding"] for _, m in rows))
    print()
    print(format_metrics(metrics, f"等权组合 | {args.exchange} {args.market} {args.timeframe} | "
                                  f"{len(symbols)} 个币 × {weight:.0%} | {len(port_equity)} 根 K 线"))
    print(f"{'等权买入持有':<16}{metrics['benchmark_return']:>+20.2%}")
    print(f"\n分散效果：组合波动 {port_equity.pct_change().std():.4%}/根，"
          f"最差单币回撤 {min(m['max_drawdown'] for _, m in rows):.1%}，"
          f"组合回撤 {metrics['max_drawdown']:.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
