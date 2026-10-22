"""Evaluate directional strategies re-implemented from open-source projects
(freqtrade-strategies, jesse example-strategies, hummingbot V2 controllers) under one
set of rules, and rank them. Backtest only: nothing here talks to an exchange account.

    python tools/oss_eval.py fetch                 # Binance USDT-M klines + funding from data.binance.vision
    python tools/oss_eval.py run --workers 4       # walk-forward every strategy x symbol x timeframe x window
    python tools/oss_eval.py report                # ranking tables (markdown) from the saved results

Rules (fixed before looking at any result):
  * Binance perpetuals BTC/ETH/SOL, 4h and 1h; two windows, data from 2021-01-01 and from
    2023-01-01, both cut at 2026-10-22 00:00 UTC.
  * Costs: taker fee 0.05% per side, the configured slippage (0.05%), historical funding.
  * Risk as in config.example.yaml (1% risk per trade, 2 x ATR stop, 1x), except the
    20% drawdown breaker and the 3% daily loss stop are switched off, as in
    experiments/compare_filters.py, so a breaker doesn't freeze half the sample.
  * Parameters: each strategy's `param_grid` (<= 30 combinations, written before the
    run), chosen by walk-forward: train 365 days, test 90 days, best training Sharpe
    with at least 5 trades. Only the stitched test windows are reported.
  * Controls: BTC buy & hold, and the live strategy (MA 20/50 + ADX >= 20) with its
    fixed parameters over the same out-of-sample span.
  * "Valid" = equal-weight BTC/ETH/SOL out-of-sample return is positive in both windows.
Binance's API answers 451 from the dev machine; data.binance.vision is the public bulk
mirror of the same klines and funding history.
"""
from __future__ import annotations

import argparse
import io
import logging
import math
import pickle
import sys
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.config import StrategyConfig, load_config  # noqa: E402
from bot.data import OHLCV_COLUMNS, timeframe_ms, to_ms  # noqa: E402
from bot.engine.backtest import run_backtest  # noqa: E402
from bot.engine.optimize import _quiet, param_combos, walk_forward  # noqa: E402
from bot.metrics import compute_metrics  # noqa: E402
from bot.strategies import STRATEGIES  # noqa: E402

SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
TIMEFRAMES = ["4h", "1h"]
WINDOWS = {"2021": "2021-01-01", "2023": "2023-01-01"}
END = "2026-10-22"
CANDIDATES = ["rsi2", "bb_reversion", "bb_breakout", "dual_thrust", "vol_system"]
BASELINE = ("ma_cross", {"fast": 20, "slow": 50, "adx_min": 20})
TRAIN_DAYS, TEST_DAYS = 365, 90

SOURCES = {
    "rsi2": "https://github.com/jesse-ai/example-strategies/tree/master/RSI2",
    "bb_reversion": "https://github.com/hummingbot/hummingbot/blob/master/controllers/directional_trading/bollinger_v1.py",
    "bb_breakout": "https://github.com/jesse-ai/example-strategies/tree/master/SimpleBollinger",
    "dual_thrust": "https://github.com/jesse-ai/example-strategies/tree/master/DUAL_THRUST",
    "vol_system": "https://github.com/freqtrade/freqtrade-strategies/blob/main/user_data/strategies/futures/VolatilitySystem.py",
}

VISION = "https://data.binance.vision/data/futures/um"
DATA_DIR = Path("data")
RESULTS = Path("reports/oss_eval/results.pkl")
log = logging.getLogger("oss_eval")


# ---------------------------------------------------------------- data

def _csv_path(symbol: str, kind: str) -> Path:
    base = symbol.split("/")[0]
    suffix = "_ohlcv_1h" if kind == "ohlcv" else "_funding"
    return DATA_DIR / f"binance_perp_{base}-USDT_USDT{suffix}.csv"


def _download(url: str, dest: Path) -> bytes | None:
    if dest.exists():
        return dest.read_bytes()
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            blob = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(blob)
    return blob


def _read_zip_csv(blob: bytes, names: list[str]) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        raw = z.read(z.namelist()[0]).decode()
    first = raw.split("\n", 1)[0]
    header = 0 if not first[:1].isdigit() else None   # older files have no header row
    df = pd.read_csv(io.StringIO(raw), header=header)
    df = df.iloc[:, :len(names)]
    df.columns = names
    return df


def _merge_save(path: Path, new: pd.DataFrame, key) -> pd.DataFrame:
    """Merge with whatever is already cached; `key` maps timestamps to a dedupe key."""
    if path.exists():
        new = pd.concat([pd.read_csv(path), new], ignore_index=True)
    new = new.assign(_k=key(new["timestamp"])).drop_duplicates("_k", keep="last").drop(columns="_k")
    new = new.sort_values("timestamp").reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    new.to_csv(path, index=False)
    return new


def fetch(since: str, until: str) -> None:
    months = pd.period_range(pd.Timestamp(since), pd.Timestamp(until) - pd.Timedelta(days=1), freq="M")
    raw_dir = DATA_DIR / "binance_vision"
    for symbol in SYMBOLS:
        pair = symbol.replace("/", "")
        klines, funding, covered = [], [], set()
        for m in months:
            blob = _download(f"{VISION}/monthly/klines/{pair}/1h/{pair}-1h-{m}.zip",
                             raw_dir / f"{pair}-1h-{m}.zip")
            if blob is not None:
                klines.append(_read_zip_csv(blob, OHLCV_COLUMNS))
                covered.add(str(m))
            blob = _download(f"{VISION}/monthly/fundingRate/{pair}/{pair}-fundingRate-{m}.zip",
                             raw_dir / f"{pair}-fundingRate-{m}.zip")
            if blob is not None:
                f = _read_zip_csv(blob, ["timestamp", "interval", "rate"])
                funding.append(f[["timestamp", "rate"]])
        # months without a monthly file yet (the current one) come as daily files
        for day in pd.date_range(pd.Timestamp(since), pd.Timestamp(until) - pd.Timedelta(days=1), freq="D"):
            if day.strftime("%Y-%m") in covered:
                continue
            d = day.strftime("%Y-%m-%d")
            blob = _download(f"{VISION}/daily/klines/{pair}/1h/{pair}-1h-{d}.zip", raw_dir / f"{pair}-1h-{d}.zip")
            if blob is not None:
                klines.append(_read_zip_csv(blob, OHLCV_COLUMNS))
        k = pd.concat(klines, ignore_index=True).astype({"timestamp": "int64"})
        k = k[(k["timestamp"] >= to_ms(since)) & (k["timestamp"] < to_ms(until))]
        k = _merge_save(_csv_path(symbol, "ohlcv"), k, lambda ts: ts)
        f = pd.concat(funding, ignore_index=True).astype({"timestamp": "int64", "rate": "float64"})
        f = f[(f["timestamp"] >= to_ms(since)) & (f["timestamp"] < to_ms(until))]
        # API and bulk files stamp the same settlement a few ms apart: dedupe per minute
        f = _merge_save(_csv_path(symbol, "funding"), f, lambda ts: ts // 60000)
        gaps = int((np.diff(k["timestamp"]) > 3600 * 1000).sum())
        print(f"{symbol}: {len(k)} 根 1h K 线 {_day(k.timestamp.iloc[0])} ~ {_day(k.timestamp.iloc[-1])}，"
              f"缺口 {gaps} 处；资金费 {len(f)} 条，最后一条 {_day(f.timestamp.iloc[-1])}")


def _day(ts: int) -> str:
    return str(pd.Timestamp(int(ts), unit="ms", tz="UTC"))[:16]


def load_bars(symbol: str, timeframe: str, since: str, until: str) -> pd.DataFrame:
    df = pd.read_csv(_csv_path(symbol, "ohlcv"))[OHLCV_COLUMNS]
    df = df[(df["timestamp"] >= to_ms(since)) & (df["timestamp"] < to_ms(until))]
    if timeframe != "1h":
        step = timeframe_ms(timeframe)
        g = df.groupby(df["timestamp"] // step * step)
        df = pd.DataFrame({"timestamp": g["timestamp"].first() // step * step, "open": g["open"].first(),
                           "high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last(),
                           "volume": g["volume"].sum()})
    return df.reset_index(drop=True).astype({"timestamp": "int64"})


def load_funding(symbol: str, since: str, until: str) -> pd.DataFrame:
    f = pd.read_csv(_csv_path(symbol, "funding"))
    return f[(f["timestamp"] >= to_ms(since)) & (f["timestamp"] < to_ms(until))].reset_index(drop=True)


# ---------------------------------------------------------------- evaluation

def make_cfg(symbol: str, timeframe: str, strategy: str, params: dict):
    cfg = load_config("config.example.yaml", {"exchange": "binance", "market": "perp",
                                              "symbol": symbol, "timeframe": timeframe})
    return replace(cfg, strategy=StrategyConfig(strategy, params),
                   risk=replace(cfg.risk, max_drawdown=0.99, daily_loss_limit=0.99))


def _summary(m: dict) -> dict:
    keys = ("total_return", "sharpe", "max_drawdown", "trades", "fees", "funding", "win_rate", "cagr")
    return {k: m[k] for k in keys}


def evaluate(job: tuple[str, str, str, str]) -> dict:
    """Walk-forward one strategy on one symbol / timeframe / window, plus the controls
    over exactly the same out-of-sample span."""
    strategy, symbol, timeframe, window = job
    logging.basicConfig(level=logging.ERROR)
    since = WINDOWS[window]
    df = load_bars(symbol, timeframe, since, END)
    funding = load_funding(symbol, since, END)
    cfg = make_cfg(symbol, timeframe, strategy, {})
    grid = STRATEGIES[strategy].param_grid
    wf = walk_forward(cfg, df, grid, funding, TRAIN_DAYS, TEST_DAYS, "sharpe", min_trades=5)

    first_test = int(wf.equity.index[0])
    span = df[df["timestamp"] >= first_test - 400 * timeframe_ms(timeframe)]  # room for warm-up
    with _quiet():
        base = run_backtest(make_cfg(symbol, timeframe, *BASELINE), span, funding, start_ts=first_test)
    return {
        "strategy": strategy, "symbol": symbol, "timeframe": timeframe, "window": window,
        "grid_size": len(param_combos(grid)), "n_windows": len(wf.windows),
        "oos": _summary(wf.metrics), "equity": wf.equity,
        "fixed_default": _summary(wf.fixed.metrics), "hindsight": _summary(wf.hindsight_result.metrics),
        "hindsight_params": wf.hindsight.params,
        "picks": [w.params for w in wf.windows],
        "window_returns": [w.test_return for w in wf.windows],
        "baseline": _summary(base.metrics), "baseline_equity": base.equity,
        "buy_hold": float(wf.benchmark.iloc[-1] / cfg.initial_equity - 1), "buy_hold_equity": wf.benchmark,
        "initial_equity": cfg.initial_equity, "bar_ms": timeframe_ms(timeframe),
    }


def run(workers: int, only: list[str] | None) -> None:
    strategies = only or CANDIDATES
    jobs = [(s, sym, tf, w) for s in strategies for tf in TIMEFRAMES for w in WINDOWS for sym in SYMBOLS]
    results = []
    if RESULTS.exists():  # keep results of strategies not re-run this time
        results = [r for r in pickle.loads(RESULTS.read_bytes()) if r["strategy"] not in strategies]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for r in pool.map(evaluate, jobs):
            o = r["oos"]
            print(f"{r['strategy']:13s} {r['symbol']:9s} {r['timeframe']} {r['window']}  "
                  f"样本外 {o['total_return']:+8.1%}  夏普 {o['sharpe']:5.2f}  回撤 {o['max_drawdown']:6.1%}  "
                  f"{o['trades']:4d} 笔", flush=True)
            results.append(r)
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_bytes(pickle.dumps(results))
    print(f"结果已写入 {RESULTS}")


# ---------------------------------------------------------------- report

def _combined_curve(rows: list[dict], key: str) -> pd.Series:
    """Equal-weight BTC/ETH/SOL: one account split in three, each third compounding on its own."""
    curves = pd.concat([r[key] for r in rows], axis=1).sort_index().ffill().dropna()
    total = curves.sum(axis=1) / len(rows)
    total.index = pd.to_datetime(total.index, unit="ms", utc=True)
    return total


def _yearly(eq: pd.Series) -> pd.Series:
    year_end = eq.groupby(eq.index.year).last()
    return year_end / year_end.shift(1).fillna(eq.iloc[0]) - 1


def combine(rows: list[dict], key: str = "equity", trades_key: str = "oos") -> dict:
    curves = pd.concat([r[key] for r in rows], axis=1).sort_index().ffill().dropna()
    initial = rows[0]["initial_equity"]
    total = curves.sum(axis=1) / len(rows)
    m = compute_metrics(total, [], initial, rows[0]["bar_ms"])
    return {"total_return": m["total_return"], "sharpe": m["sharpe"], "max_drawdown": m["max_drawdown"],
            "cagr": m["cagr"],
            "trades": sum(r[trades_key]["trades"] for r in rows) if trades_key else None,
            # fees / funding in % of the whole account (each symbol traded a third of it)
            "fees": sum(r[trades_key]["fees"] for r in rows) / len(rows) / initial if trades_key else None,
            "funding": sum(r[trades_key]["funding"] for r in rows) / len(rows) / initial if trades_key else None,
            "start": int(total.index[0]), "end": int(total.index[-1])}


def _pct(x) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:+.1%}"


def _num(x) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.2f}"


def report() -> str:
    results = pickle.loads(RESULTS.read_bytes())
    groups: dict[tuple, list[dict]] = {}
    for r in results:
        groups.setdefault((r["strategy"], r["timeframe"], r["window"]), []).append(r)
    combos = {k: combine(v) for k, v in groups.items()}
    out = []

    # controls, per timeframe / window (same span as the candidates' out-of-sample)
    out.append("### 对照组（同一样本外区间）\n")
    out.append("| 周期 | 窗口 | 样本外区间 | BTC 买入持有 | BTC/ETH/SOL 等权买入持有 | 均线 20/50+ADX≥20 等权 | 夏普 | 回撤 | 笔数 |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    for tf in TIMEFRAMES:
        for w in WINDOWS:
            rows = groups.get((CANDIDATES[0], tf, w)) or next(v for (s, t, ww), v in groups.items() if t == tf and ww == w)
            btc = next(r for r in rows if r["symbol"] == "BTC/USDT")
            bh = combine(rows, "buy_hold_equity", None)
            ma = combine(rows, "baseline_equity", "baseline")
            out.append(f"| {tf} | {w} | {_day(bh['start'])[:10]} ~ {_day(bh['end'])[:10]} | {_pct(btc['buy_hold'])} | "
                       f"{_pct(bh['total_return'])} | {_pct(ma['total_return'])} | {_num(ma['sharpe'])} | "
                       f"{_pct(ma['max_drawdown'])} | {ma['trades']} |")

    out.append("\n### 排名（BTC/ETH/SOL 等权，walk-forward 样本外）\n")
    out.append("按两个窗口里较差那个的样本外夏普排序。“有效”= 两个窗口样本外收益都为正。\n")
    out.append("| # | 策略 | 周期 | 网格 | 2021 起：收益 / 夏普 / 回撤 / 笔数 / 手续费 / 资金费 | "
               "2023 起：收益 / 夏普 / 回撤 / 笔数 / 手续费 / 资金费 | 有效 |")
    out.append("|---|---|---|---|---|---|---|")
    keys = sorted({(s, tf) for s, tf, _ in groups}, key=lambda k: -min(
        _nan_low(combos.get((k[0], k[1], w), {}).get("sharpe")) for w in WINDOWS))
    for rank, (s, tf) in enumerate(keys, 1):
        cells, valid = [], True
        for w in WINDOWS:
            c = combos.get((s, tf, w))
            if c is None:
                cells.append("—")
                valid = False
                continue
            valid &= c["total_return"] > 0
            cells.append(f"{_pct(c['total_return'])} / {_num(c['sharpe'])} / {_pct(c['max_drawdown'])} / "
                         f"{c['trades']} / {c['fees']:.1%} / {c['funding']:.1%}")
        grid = groups[(s, tf, "2021")][0]["grid_size"] if (s, tf, "2021") in groups else "?"
        out.append(f"| {rank} | `{s}` | {tf} | {grid} | {cells[0]} | {cells[1]} | {'是' if valid else '否'} |")

    # does it add anything to the live strategy? yearly path + correlation with MA 20/50 (4h)
    out.append("\n### 2021 窗口样本外按年拆开（等权）；与线上均线 4h 的日收益相关性；和均线 4h 各半的组合\n")
    span = [pd.Timestamp(int(t), unit="ms", tz="UTC").year
            for r in results if r["window"] == "2021" for t in r["equity"].index[[0, -1]]]
    years = list(range(min(span), max(span) + 1))
    out.append("| 策略 | 周期 | " + " | ".join(str(y) for y in years) +
               " | 相关性 2021 / 2023 | 各半组合夏普 2021 / 2023（均线单独 " +
               " / ".join(_num(combine(groups[(CANDIDATES[0], '4h', w)], 'baseline_equity', 'baseline')['sharpe'])
                          for w in WINDOWS) + "） |")
    out.append("|---|---|" + "---|" * len(years) + "---|---|")
    controls = [("均线 20/50+ADX≥20", "4h", "baseline_equity"), ("等权买入持有", "4h", "buy_hold_equity")]
    for label, tf, key in controls:
        yearly = _yearly(_combined_curve(groups[(CANDIDATES[0], tf, "2021")], key))
        out.append(f"| {label} | {tf} | " + " | ".join(_pct(yearly.get(y)) for y in years) + " | — | — |")
    for s, tf in keys:
        rows = groups.get((s, tf, "2021"))
        if not rows:
            continue
        yearly = _yearly(_combined_curve(rows, "equity"))
        corr, blend = [], []
        for w in WINDOWS:
            cand = _combined_curve(groups[(s, tf, w)], "equity")
            ma = _combined_curve(groups[(CANDIDATES[0], "4h", w)], "baseline_equity")
            daily = pd.concat([cand.resample("1D").last(), ma.resample("1D").last()], axis=1).dropna()
            rets = daily.pct_change().dropna()
            corr.append(_num(rets.iloc[:, 0].corr(rets.iloc[:, 1])))
            half = rets.mean(axis=1)   # rebalanced daily to 50/50
            blend.append(_num(half.mean() / half.std() * math.sqrt(365)))
        out.append(f"| `{s}` | {tf} | " + " | ".join(_pct(yearly.get(y)) for y in years) +
                   f" | {' / '.join(corr)} | {' / '.join(blend)} |")

    out.append("\n### 分币种（样本外收益 / 夏普；括号里是默认参数固定不动、事后最优参数的收益）\n")
    out.append("| 策略 | 周期 | 窗口 | BTC | ETH | SOL | 选参窗口数 | 试过的组合（网格×窗口） |")
    out.append("|---|---|---|---|---|---|---|---|")
    for (s, tf, w), rows in sorted(groups.items()):
        by = {r["symbol"]: r for r in rows}
        cells = []
        for sym in SYMBOLS:
            r = by.get(sym)
            cells.append("—" if r is None else
                         f"{_pct(r['oos']['total_return'])} / {_num(r['oos']['sharpe'])} "
                         f"({_pct(r['fixed_default']['total_return'])}, {_pct(r['hindsight']['total_return'])})")
        n_win = rows[0]["n_windows"]
        out.append(f"| `{s}` | {tf} | {w} | {' | '.join(cells)} | {n_win} | "
                   f"{rows[0]['grid_size']}×{n_win}×{len(rows)} = {rows[0]['grid_size'] * n_win * len(rows)} |")

    out.append("\n### walk-forward 每个窗口选中的参数（出现次数，三个币合计）\n")
    for (s, tf, w), rows in sorted(groups.items()):
        counts: dict[str, int] = {}
        for r in rows:
            for p in r["picks"]:
                label = "空仓（训练期不足 5 笔）" if p is None else ", ".join(f"{k}={v}" for k, v in p.items())
                counts[label] = counts.get(label, 0) + 1
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:4]
        out.append(f"- `{s}` {tf} {w}：" + "；".join(f"{k} ×{n}" for k, n in top))
    return "\n".join(out)


def _nan_low(x) -> float:
    return -99.0 if x is None or not math.isfinite(x) else float(x)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="从 data.binance.vision 下载 1h K 线和资金费")
    f.add_argument("--since", default="2021-01-01")
    f.add_argument("--until", default=END)
    r = sub.add_parser("run", help="跑 walk-forward，结果存到 reports/oss_eval/")
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--strategies", help="逗号分隔，默认全部候选")
    sub.add_parser("report", help="从保存的结果出排名表（markdown）")
    args = ap.parse_args()
    if args.cmd == "fetch":
        fetch(args.since, args.until)
    elif args.cmd == "run":
        run(args.workers, args.strategies.split(",") if args.strategies else None)
    else:
        print(report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
