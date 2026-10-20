"""前向验证的阶段报告：把 SQLite 账本里的权益曲线和同期买入持有放在一起比。

    python tools/forward_report.py
    python tools/forward_report.py --db data/forward.db --symbol BTC/USDT:USDT --timeframe 4h --out reports/forward

判读标准（事先定好，别事后找理由）：见 HANDOVER「前向验证」一节。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.data import fetch_ohlcv_range, make_exchange, timeframe_ms  # noqa: E402
from bot.metrics import compute_metrics  # noqa: E402
from bot.report import format_metrics  # noqa: E402

DEFAULT_DB = "data/portfolio.db"
DEFAULT_RUN = "testnet:binance:perp:BTC/USDT:USDT:4h:ma_cross"


def load_ledger(db: str, run: str):
    conn = sqlite3.connect(db)
    equity = conn.execute("SELECT ts, equity FROM equity WHERE run = ? ORDER BY ts", (run,)).fetchall()
    fills = conn.execute(
        "SELECT ts, side, qty, price, fee, pnl, reason, run FROM fills WHERE run = ? ORDER BY id", (run,)).fetchall()
    return equity, fills


def main() -> int:
    ap = argparse.ArgumentParser(description="前向验证报告")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--run", default=DEFAULT_RUN)
    ap.add_argument("--exchange", default="binance")
    ap.add_argument("--symbol", default="BTC/USDT:USDT")
    ap.add_argument("--timeframe", default="4h")
    ap.add_argument("--out", default=None, help="把权益曲线与成交写成 CSV 的目录")
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"没有找到 {args.db}")
        return 1
    equity, fills = load_ledger(args.db, args.run)
    if len(equity) < 2:
        print(f"只有 {len(equity)} 个权益点，还写不了报告（等几根 K 线）")
        return 1

    eq = pd.Series([e for _, e in equity], index=pd.Index([t for t, _ in equity], name="timestamp"),
                   dtype=float)
    first_ts, last_ts = int(eq.index[0]), int(eq.index[-1])
    trades = [{"pnl": f[5], "reason": f[6]} for f in fills if f[5] is not None]
    fees = float(sum(f[4] for f in fills))
    if any(f[6] == "liquidation" for f in fills):
        print("⚠ 出现过强平")

    # 同期买入持有：从公开行情取同一区间，按权益点对齐
    ex = make_exchange(args.exchange, "perp")
    bars = fetch_ohlcv_range(ex, args.symbol, args.timeframe, first_ts, last_ts + timeframe_ms(args.timeframe))
    close = bars.set_index("timestamp")["close"].reindex(eq.index).ffill()
    benchmark = close / close.iloc[0] * float(eq.iloc[0])

    metrics = compute_metrics(eq, trades, float(eq.iloc[0]), timeframe_ms(args.timeframe),
                              benchmark_return=float(benchmark.iloc[-1] / eq.iloc[0] - 1), fees=fees)
    days = (last_ts - first_ts) / 86_400_000
    title = (f"前向验证 | {args.run} | {days:.1f} 天 / {len(eq)} 根 K 线 / {len(fills)} 笔成交")
    print(format_metrics(metrics, title))
    print(f"{'同期买入持有':<16}{metrics['benchmark_return']:>+20.2%}")
    if days < 14:
        print(f"（样本只有 {days:.1f} 天：年化与夏普在这个长度上没有意义，只看收益、回撤、手续费和成交原因）")

    if fills:
        notional = [f[2] * f[3] for f in fills if f[2] and f[3]]
        rate = [f[4] / n for f, n in zip([x for x in fills if x[2] and x[3]], notional) if n]
        if rate:
            print(f"\n实际手续费率 中位数 {sorted(rate)[len(rate) // 2]:.4%}（taker 基准 0.0400%）")
        print("成交原因分布 " + ", ".join(f"{k}×{v}" for k, v in
                                    pd.Series([f[6] for f in fills]).value_counts().items()))
        wins = [f[5] for f in fills if f[5] is not None]
        if wins:
            print(f"已实现盈亏合计 {sum(wins):+.2f} USDT，手续费合计 {fees:.2f} USDT")

    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"equity": eq, "buy_and_hold": benchmark}).to_csv(out / "forward_equity.csv", index_label="time")
        pd.DataFrame(fills, columns=["ts", "side", "qty", "price", "fee", "pnl", "reason", "run"]).to_csv(
            out / "forward_fills.csv", index=False)
        print(f"\n已保存 {out / 'forward_equity.csv'} 与 {out / 'forward_fills.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
