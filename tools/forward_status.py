"""前向验证的实时状态：从 SQLite 账本里读出持仓、权益、最近成交和存活情况。

    python tools/forward_status.py
    python tools/forward_status.py --db data/forward.db --run "testnet:binance:perp:BTC/USDT:USDT:4h:ma_cross"
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = "data/forward.db"
DEFAULT_RUN = "testnet:binance:perp:BTC/USDT:USDT:4h:ma_cross"
STALE_BARS = 1.5  # 超过这么多根 K 线没记录权益，就认为进程可能停了


def utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def main() -> int:
    ap = argparse.ArgumentParser(description="前向验证状态")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--run", default=DEFAULT_RUN)
    ap.add_argument("--bar-hours", type=float, default=4.0, help="K 线周期，用于判断是否失联长")
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"没有找到 {args.db}，前向验证还没启动过")
        return 1
    conn = sqlite3.connect(args.db)
    row = conn.execute("SELECT data FROM state WHERE run = ?", (args.run,)).fetchone()
    if row is None:
        print(f"{args.db} 里没有 run={args.run}")
        return 1

    trader = json.loads(row[0])["trader"]
    pos = trader["broker"].get("position")
    risk = trader["risk"]
    equity = conn.execute("SELECT ts, equity FROM equity WHERE run = ? ORDER BY ts", (args.run,)).fetchall()
    fills = conn.execute(
        "SELECT ts, side, qty, price, fee, pnl, reason FROM fills WHERE run = ? ORDER BY id", (args.run,)).fetchall()

    print(f"run      {args.run}")
    if pos:
        print(f"持仓     {pos['side']:+d} {pos['qty']:.6f} @ {pos['entry']:.2f}  "
              f"止损 {pos['stop'] if pos['stop'] is None else round(pos['stop'], 2)}  "
              f"强平 {pos['liq_price'] if pos['liq_price'] is None else round(pos['liq_price'], 2)}")
    else:
        print("持仓     无（last_signal=%+d）" % trader["last_signal"])
    print(f"风控     峰值权益 {risk['peak_equity']:.2f}  当日基准 {risk['day_start_equity']:.2f}  "
          f"熔断 {'YES: ' + str(risk['halt_reason']) if risk['halted'] else '否'}")

    if not equity:
        print("权益     还没有记录（等第一根 K 线收盘）")
        return 0

    first_ts, first_eq = equity[0]
    last_ts, last_eq = equity[-1]
    peak = max(e for _, e in equity)
    mdd = min(e / max(x for t, x in equity if t <= ts) - 1 for ts, e in equity)
    days = (last_ts - first_ts) / 86_400_000
    stale_h = (datetime.now(timezone.utc).timestamp() * 1000 - last_ts) / 3_600_000

    print(f"权益     {last_eq:.2f}（起始 {first_eq:.2f}，{last_eq / first_eq - 1:+.2%}，峰值 {peak:.2f}，回撤 {mdd:.2%}）")
    print(f"运行     {days:.1f} 天，{len(equity)} 根 K 线，{len(fills)} 笔成交")
    print(f"存活     最后一根 K 线 {utc(last_ts)} UTC（K 线开盘时间），距今 {stale_h:.1f} 小时"
          f" / 警戒线 {STALE_BARS * args.bar_hours:.0f} 小时"
          f"{'  ⚠ 超过警戒线，进程可能已停止' if stale_h > STALE_BARS * args.bar_hours else ''}")
    for f in fills[-5:]:
        ts, side, qty, price, fee, pnl, reason = f
        print(f"  {utc(ts)}  {side:<4} {qty:.6f} @ {price:,.2f}  fee {fee:.3f}  "
              f"pnl {'-' if pnl is None else format(pnl, '+.3f')}  {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
