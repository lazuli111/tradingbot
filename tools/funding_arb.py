"""资金费套利（现货多 + 永续空）可行性回测：只读历史数据，不下单。

    # 拉数据（走 bot/data.py 的本地 CSV 缓存；开发机访问 Binance 是 451，要在能直连的机器上跑）
    python tools/funding_arb.py fetch --symbols BTC/USDT,ETH/USDT --since 2023-01-01 --until 2026-10-22

    # 回测：只读本地缓存，不联网（开发机上也能跑）
    python tools/funding_arb.py report --since 2023-01-01 --until 2026-10-22 --csv reports/funding_arb.csv
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import funding_arb as fa  # noqa: E402
from bot.config import Config  # noqa: E402
from bot.data import load_funding, load_ohlcv, make_exchange  # noqa: E402

DEFAULT_SYMBOLS = ("BTC/USDT,ETH/USDT,SOL/USDT,XRP/USDT,DOGE/USDT,BNB/USDT,"
                   "ADA/USDT,AVAX/USDT,LINK/USDT,LTC/USDT,SUI/USDT")


class OfflineExchange:
    """缓存没覆盖的区间一律当作“交易所没有数据”，让 load_* 只返回缓存里的行。"""

    def fetch_ohlcv(self, *args, **kwargs):
        return []

    def fetch_funding_rate_history(self, *args, **kwargs):
        return []


def load_symbol(cfg: Config, since, until, offline: bool = False):
    """资金费 + 现货 / 永续 1h K 线，走 bot/data.py 的 CSV 缓存。"""
    perp = replace(cfg, market="perp")
    spot = replace(cfg, market="spot")
    ex_perp = OfflineExchange() if offline else make_exchange(cfg.exchange, "perp")
    ex_spot = OfflineExchange() if offline else make_exchange(cfg.exchange, "spot")
    funding = load_funding(perp, since, until, ex=ex_perp)
    perp_px = load_ohlcv(perp, since, until, ex=ex_perp)
    spot_px = load_ohlcv(spot, since, until, ex=ex_spot)
    return funding, spot_px, perp_px


def cmd_fetch(args, symbols) -> int:
    for sym in symbols:
        cfg = Config(exchange=args.exchange, symbol=sym, timeframe="1h", data_dir=args.data_dir)
        funding, spot_px, perp_px = load_symbol(cfg, args.since, args.until)
        print(f"{sym}: 资金费 {len(funding)} 条，现货 {len(spot_px)} 根，永续 {len(perp_px)} 根", flush=True)
    return 0


def cmd_report(args, symbols) -> int:
    costs = fa.Costs(spot_fee=args.spot_fee, perp_fee=args.perp_fee, slippage=args.slippage,
                     rebalance_band=args.rebalance_band)
    grid = fa.Grid(lookbacks=tuple(int(x) for x in args.lookbacks.split(",")),
                   thresholds=tuple(float(x) / 100 for x in args.thresholds.split(",")))
    rows, wf_rows, risk_rows, yearly_rows = [], [], [], []
    for sym in symbols:
        cfg = Config(exchange=args.exchange, symbol=sym, timeframe="1h", data_dir=args.data_dir)
        funding, spot_px, perp_px = load_symbol(cfg, args.since, args.until, offline=True)
        hourly = fa.align_hourly(funding, spot_px, perp_px)
        events = fa.funding_events(hourly)
        years = fa.span_years(events)

        always = fa.simulate(events, fa.always_hold(events), costs)
        row = {"symbol": sym, "start": fa.fmt_ts(events["timestamp"].iloc[0]),
               "end": fa.fmt_ts(events["timestamp"].iloc[-1]), "years": years,
               **fa.summarize(always, years, prefix="always_")}

        wf = fa.walk_forward(events, costs, grid, train_days=args.train_days, test_days=args.test_days)
        oos = wf["events"]
        oos_years = fa.span_years(oos)
        row.update(fa.summarize(fa.simulate(oos, fa.always_hold(oos), costs), oos_years, prefix="oos_always_"))
        row.update(fa.summarize(wf["result"], oos_years, prefix="oos_wf_"))
        row["oos_start"] = fa.fmt_ts(oos["timestamp"].iloc[0])
        rows.append(row)
        for w in wf["windows"]:
            wf_rows.append({"symbol": sym, **w})

        risk = fa.risk_metrics(hourly, leverages=(1, 2, 3), mmr=args.mmr)
        risk_rows.append({"symbol": sym, **risk})
        yearly_rows.append({"symbol": sym, **fa.yearly_funding_apr(events)})
        print(f"{sym}: 一直持有 净年化 {row['always_net_apr']:+.2%}（资金费 {row['always_funding_apr']:+.2%}，"
              f"基差 {row['always_basis_apr']:+.2%}，手续费 {-row['always_fee_apr']:.2%}）；"
              f"样本外 {row['oos_start']} 起 一直持有 {row['oos_always_net_apr']:+.2%} / "
              f"walk-forward {row['oos_wf_net_apr']:+.2%}（持有 {row['oos_wf_time_in_market']:.0%} 时间，"
              f"{row['oos_wf_trades']} 次开仓）", flush=True)

    table = pd.DataFrame(rows)
    risk_table = pd.DataFrame(risk_rows)
    print()
    yearly = pd.DataFrame(yearly_rows)
    print(fa.format_table(table, risk_table, leverage=args.leverage, yearly=yearly))
    if args.csv:
        out = Path(args.csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        table.to_csv(out, index=False)
        pd.DataFrame(wf_rows).to_csv(out.with_name(out.stem + "_wf.csv"), index=False)
        risk_table.to_csv(out.with_name(out.stem + "_risk.csv"), index=False)
        yearly.to_csv(out.with_name(out.stem + "_yearly.csv"), index=False)
        print(f"\n已写入 {out} 及 _wf / _risk / _yearly 三个附表")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="资金费套利（现货多 + 永续空）可行性回测")
    ap.add_argument("command", choices=["fetch", "report"])
    ap.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    ap.add_argument("--exchange", default="binance")
    ap.add_argument("--since", default="2023-01-01")
    ap.add_argument("--until", default=None, help="默认到现在；report 只读缓存，给定后结果可复现")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--spot-fee", type=float, default=0.001, help="现货 taker 费率")
    ap.add_argument("--perp-fee", type=float, default=0.0005, help="永续 taker 费率")
    ap.add_argument("--slippage", type=float, default=0.0, help="每条腿每次成交的额外滑点")
    ap.add_argument("--rebalance-band", type=float, default=0.1,
                    help="名义偏离 1 超过这个比例就把两条腿调回 1 单位名义（付手续费）")
    ap.add_argument("--mmr", type=float, default=0.004, help="永续维持保证金率（算强平距离）")
    ap.add_argument("--leverage", type=float, default=1.0,
                    help="永续空单杠杆，只影响“按占用资本”那一列：资本 = 名义 × (1 + 1/杠杆)")
    ap.add_argument("--lookbacks", default="3,9,21,42,90", help="walk-forward 候选 N（资金费周期数）")
    ap.add_argument("--thresholds", default="0,5,10,15,20,30", help="walk-forward 候选阈值（年化 %%）")
    ap.add_argument("--train-days", type=int, default=180)
    ap.add_argument("--test-days", type=int, default=90)
    ap.add_argument("--csv", default=None)
    args = ap.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    return cmd_fetch(args, symbols) if args.command == "fetch" else cmd_report(args, symbols)


if __name__ == "__main__":
    sys.exit(main())
