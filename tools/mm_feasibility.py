"""盘口做市可行性：读 bot.mm.recorder 录下的数据，逐品种输出价差、成交频率、markout 和每个来回的期望。

    python tools/mm_feasibility.py --data data/mm
    python tools/mm_feasibility.py --data data/mm --symbols SOLUSDT,ADAUSDT --maker-fee 0.00018 --csv reports/mm.csv

口径见 bot/mm/feasibility.py 的模块说明。费率用小数（0.0002 = 0.02% = 2bp）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.mm.feasibility import Params, analyze, coverage_report  # noqa: E402


def fmt_table(df: pd.DataFrame, cols: dict[str, str], digits: int = 2) -> str:
    head = "| " + " | ".join(cols.values()) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    lines = [head, sep]
    for _, r in df.iterrows():
        cells = []
        for c in cols:
            v = r[c]
            if isinstance(v, float):
                cells.append(f"{v:,.{digits}f}" if abs(v) < 1e5 else f"{v:,.0f}")
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="盘口做市可行性分析")
    ap.add_argument("--data", default="data/mm", help="recorder 的输出目录")
    ap.add_argument("--symbols", help="逗号分隔（默认目录里全部品种）")
    ap.add_argument("--maker-fee", type=float, default=0.0002, help="maker 费率，小数（默认 0.0002）")
    ap.add_argument("--horizons", default="1,5,30,60", help="markout 秒数")
    ap.add_argument("--verdict-horizon", type=int, default=30, help="结论用哪个 horizon 的逆向选择")
    ap.add_argument("--chunk-hours", type=int, default=24, help="每次读多少小时（控制内存）")
    ap.add_argument("--csv", help="把完整结果另存为 CSV")
    args = ap.parse_args()

    horizons = tuple(int(h) for h in args.horizons.split(","))
    if args.verdict_horizon not in horizons:
        ap.error("--verdict-horizon 必须在 --horizons 里")
    params = Params(maker_fee=args.maker_fee, horizons=horizons, verdict_horizon=args.verdict_horizon,
                    chunk_hours=args.chunk_hours)
    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
    root = Path(args.data)

    cov = coverage_report(root, params.gap_margin_ms)
    df = analyze(root, symbols, params, log=lambda m: print(m, file=sys.stderr))
    if df.empty:
        print("没有可分析的数据")
        return 1
    if args.csv:
        Path(args.csv).parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(args.csv, index=False)

    h = params.verdict_horizon
    print(f"录制覆盖：{cov.get('covered_h', 0):.1f} 小时（跨度 {cov.get('span_h', 0):.1f} 小时，"
          f"{cov.get('segments', 0)} 段，空档合计 {cov.get('gap_s_total', 0):.0f} 秒，"
          f"最长 {cov.get('gap_s_max', 0):.0f} 秒）；maker 费 {params.fee_bp:.2f}bp\n")
    print("## 价差与成交\n")
    print(fmt_table(df, {
        "symbol": "品种", "tick_bp": "1 tick (bp)", "spread_med_ticks": "价差中位 (tick)",
        "spread_med_bp": "价差中位 (bp)", "spread_p90_bp": "价差 p90 (bp)", "one_tick_time": "1 tick 时间占比",
        "trades_per_h": "成交/小时", "touch_share": "touch 占比", "bid_hits_per_h": "打买一/小时",
        "ask_lifts_per_h": "吃卖一/小时", "cleared_per_h": "整档吃光/小时",
        "depth_bid_usd": "买一挂单额 (USD)", "touch_usd_per_h": "touch 成交额/小时 (USD)",
        "queue_turnover_per_h": "最优档换手/小时",
    }))
    print("\n## markout（被动方，bp；乐观口径 = 所有 touch 成交）\n")
    cols = {"symbol": "品种", "fill_spread_bp": "成交时价差"}
    for hh in horizons:
        cols[f"touch_adverse_{hh}s"] = f"逆向 {hh}s"
    for hh in horizons:
        cols[f"touch_markout_{hh}s"] = f"markout {hh}s"
    print(fmt_table(df, cols))
    print(f"\n## 每个来回期望（bp）= 2×markout − 2×maker 费；± 为 2 倍小时聚类标准误\n")
    df["rt_touch"] = [f"{a:+.2f} ± {2 * b:.2f}" for a, b in zip(df[f"touch_rt_{h}s"], df[f"touch_rt_se_{h}s"])]
    df["rt_cleared"] = [f"{a:+.2f}" for a in df[f"cleared_rt_{h}s"]]
    rt_cols = {"symbol": "品种"}
    for hh in horizons:
        rt_cols[f"touch_rt_{hh}s"] = f"乐观 {hh}s"
    rt_cols.update({"rt_touch": f"乐观 {h}s（±2SE）", "rt_cleared": f"悲观 {h}s",
                    "breakeven_fee_bp": "打平所需 maker 费 (bp)", "verdict": "结论"})
    print(fmt_table(df, rt_cols))
    return 0


if __name__ == "__main__":
    sys.exit(main())
