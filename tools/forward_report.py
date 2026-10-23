"""前向验证的阶段报告：把 SQLite 账本里的权益曲线和同期买入持有放在一起比。

    python tools/forward_report.py
    python tools/forward_report.py --db data/forward.db --symbol BTC/USDT:USDT --timeframe 4h --out reports/forward

判读标准（事先定好，别事后找理由）：见 HANDOVER「前向验证」一节。
滑点：每笔成交记着决策价（止损 / 止盈另记触发价），报告给出 bp 统计并对照验收线；
这些列是 2026-10-23 加的，之前的成交没有参考价，不参与统计。
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
FILL_COLUMNS = ("ts", "side", "qty", "price", "fee", "pnl", "reason", "ref_price", "trigger_price", "order_id")

# 滑点验收线：市价单的假设是回测里的 slippage 0.0005（5 bp）
MAX_MEAN_SLIPPAGE_BPS = 5.0
MAX_SINGLE_SLIPPAGE_BPS = 20.0
MAX_MEAN_STOP_SLIPPAGE_BPS = 10.0
MARKET_REASONS = ("signal", "circuit_breaker")


def load_ledger(db: str, run: str):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    equity = conn.execute("SELECT ts, equity FROM equity WHERE run = ? ORDER BY ts", (run,)).fetchall()
    # 账本是 C10 之前建的、机器人还没重启加列的话，新列按 NULL 读
    have = {row[1] for row in conn.execute("PRAGMA table_info(fills)")}
    cols = ", ".join(c if c in have else f"NULL AS {c}" for c in FILL_COLUMNS)
    fills = [dict(zip(FILL_COLUMNS, row)) for row in
             conn.execute(f"SELECT {cols} FROM fills WHERE run = ? ORDER BY id", (run,))]
    return equity, fills


def slippage_bps(side: str, price: float, ref: float) -> float:
    """成交价相对参考价的滑点，正数 = 对我们不利（买贵了 / 卖便宜了）。"""
    return (1 if side == "buy" else -1) * (price / ref - 1) * 1e4


def _summary(values: list[float]) -> str:
    s = pd.Series(values)
    return f"{len(s)} 笔  平均 {s.mean():+.1f}  中位 {s.median():+.1f}  最差 {s.max():+.1f} bp"


def slippage_report(fills: list[dict], bar_open: dict[int, float], step: int) -> list[str]:
    """市价单对决策价、对所在 K 线开盘价（回测口径），止损 / 止盈单对触发价。
    成交价是估算出来的、或者盈亏靠和交易所核对得出的，都不算：那个价格不是真成交价。"""
    usable = [f for f in fills if f["price"] and not any(k in (f["reason"] or "") for k in ("估算", "核对"))]
    market = [f for f in usable if f["reason"] in MARKET_REASONS and f["ref_price"]]
    exits = [f for f in usable if f["trigger_price"]]
    lines = []
    skipped = sum(1 for f in fills if f["ref_price"] is None and f["trigger_price"] is None)
    if skipped:
        lines.append(f"（{skipped} 笔没有参考价：开始记录滑点之前的成交、成交价是估算的，或强平）")

    slips = [slippage_bps(f["side"], f["price"], f["ref_price"]) for f in market]
    if slips:
        lines.append("市价单 vs 决策价     " + _summary(slips))
    opens = [(f, bar_open.get(f["ts"] - f["ts"] % step)) for f in market]
    vs_open = [slippage_bps(f["side"], f["price"], o) for f, o in opens if o]
    if vs_open:
        lines.append("市价单 vs K 线开盘价 " + _summary(vs_open) + "（回测按开盘价 + 5 bp 成交）")
    stops = [slippage_bps(f["side"], f["price"], f["trigger_price"]) for f in exits]
    if stops:
        lines.append("止损/止盈 vs 触发价  " + _summary(stops))
    if not (slips or stops):
        lines.append("还没有可以算滑点的成交")
        return lines

    checks = []
    if slips:
        mean, worst = sum(slips) / len(slips), max(slips)
        checks.append(("市价单平均滑点", mean <= MAX_MEAN_SLIPPAGE_BPS, f"{mean:+.1f} ≤ {MAX_MEAN_SLIPPAGE_BPS:g} bp"))
        checks.append(("市价单单笔滑点", worst <= MAX_SINGLE_SLIPPAGE_BPS, f"{worst:+.1f} ≤ {MAX_SINGLE_SLIPPAGE_BPS:g} bp"))
    if stops:
        mean = sum(stops) / len(stops)
        checks.append(("止损单平均滑点", mean <= MAX_MEAN_STOP_SLIPPAGE_BPS,
                       f"{mean:+.1f} ≤ {MAX_MEAN_STOP_SLIPPAGE_BPS:g} bp"))
    lines += [f"  {'✓' if ok else '✗'} {name} {detail}" for name, ok, detail in checks]
    return lines


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
    trades = [{"pnl": f["pnl"], "reason": f["reason"]} for f in fills if f["pnl"] is not None]
    fees = float(sum(f["fee"] for f in fills))
    if any(f["reason"] == "liquidation" for f in fills):
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
        rate = [f["fee"] / (f["qty"] * f["price"]) for f in fills if f["qty"] and f["price"] and f["fee"]]
        if rate:
            print(f"\n实际手续费率 中位数 {sorted(rate)[len(rate) // 2]:.4%}（taker 基准 0.0400%）")
        print("成交原因分布 " + ", ".join(f"{k}×{v}" for k, v in
                                    pd.Series([f["reason"] for f in fills]).value_counts().items()))
        wins = [f["pnl"] for f in fills if f["pnl"] is not None]
        if wins:
            print(f"已实现盈亏合计 {sum(wins):+.2f} USDT，手续费合计 {fees:.2f} USDT")
        bar_open = dict(zip(bars["timestamp"].astype(int), bars["open"].astype(float)))
        print("\n滑点（bp，正数 = 不利）")
        for line in slippage_report(fills, bar_open, timeframe_ms(args.timeframe)):
            print("  " + line)

    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"equity": eq, "buy_and_hold": benchmark}).to_csv(out / "forward_equity.csv", index_label="time")
        pd.DataFrame(fills, columns=list(FILL_COLUMNS)).to_csv(out / "forward_fills.csv", index=False)
        print(f"\n已保存 {out / 'forward_equity.csv'} 与 {out / 'forward_fills.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
