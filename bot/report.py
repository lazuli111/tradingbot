"""Console summary and CSV / PNG output for backtests."""
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd

LABELS = [
    ("final_equity", "期末权益", "{:,.2f}"),
    ("total_return", "总收益", "{:+.2%}"),
    ("benchmark_return", "买入持有收益", "{:+.2%}"),
    ("cagr", "年化收益", "{:+.2%}"),
    ("sharpe", "夏普比率", "{:.2f}"),
    ("max_drawdown", "最大回撤", "{:.2%}"),
    ("trades", "交易次数", "{}"),
    ("win_rate", "胜率", "{:.1%}"),
    ("profit_factor", "盈亏比(总盈/总亏)", "{:.2f}"),
    ("avg_trade_pnl", "平均每笔盈亏", "{:,.2f}"),
    ("fees", "手续费合计", "{:,.2f}"),
    ("funding", "资金费净支出", "{:,.2f}"),
    ("liquidations", "强平次数", "{}"),
]


def format_metrics(metrics: dict, title: str) -> str:
    lines = [title, "-" * 40]
    for key, label, fmt in LABELS:
        value = metrics.get(key)
        if value is None or (isinstance(value, float) and math.isnan(value)):
            text = "-"
        else:
            text = fmt.format(value)
        lines.append(f"{label:<16}{text:>20}")
    return "\n".join(lines)


def save_report(result, out_dir: str | Path, name: str, plot: bool = True) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []

    eq = result.equity.rename("equity").to_frame()
    eq.index = pd.to_datetime(eq.index, unit="ms", utc=True)
    eq["benchmark"] = result.benchmark.to_numpy()
    eq_path = out / f"{name}_equity.csv"
    eq.to_csv(eq_path, index_label="time")
    paths.append(eq_path)

    trades = pd.DataFrame(result.trades)
    if not trades.empty:
        for col in ("entry_ts", "exit_ts"):
            trades[col] = pd.to_datetime(trades[col], unit="ms", utc=True)
    trades_path = out / f"{name}_trades.csv"
    trades.to_csv(trades_path, index=False)
    paths.append(trades_path)

    if plot:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 6), sharex=True,
                                       gridspec_kw={"height_ratios": [3, 1]})
        ax1.plot(eq.index, eq["equity"], label="strategy", linewidth=1.2)
        ax1.plot(eq.index, eq["benchmark"], label="buy & hold", linewidth=1, alpha=0.7)
        ax1.set_ylabel("equity")
        ax1.set_title(name)
        ax1.legend()
        ax1.grid(alpha=0.3)
        dd = eq["equity"] / eq["equity"].cummax() - 1
        ax2.fill_between(eq.index, dd, 0, color="tab:red", alpha=0.4)
        ax2.set_ylabel("drawdown")
        ax2.grid(alpha=0.3)
        fig.tight_layout()
        png_path = out / f"{name}.png"
        fig.savefig(png_path, dpi=110)
        plt.close(fig)
        paths.append(png_path)
    return paths


def _day(ts: int) -> str:
    return pd.Timestamp(ts, unit="ms", tz="UTC").strftime("%Y-%m-%d")


def _params_text(params: dict | None) -> str:
    return " ".join(f"{k}={v}" for k, v in params.items()) if params else "（空仓）"


def format_walkforward(wf, title: str) -> str:
    lines = [title, "=" * 92,
             f"{'训练区间':<23}{'测试区间':<23}{'选中参数':<20}{'训练' + wf.objective:>10}"
             f"{'测试收益':>9}{'买入持有':>9}{'交易':>6}"]
    for w in wf.windows:
        score = f"{w.train_score:.2f}" if math.isfinite(w.train_score) else "-"
        lines.append(
            f"{_day(w.train_start)} ~ {_day(w.test_start):<10}  {_day(w.test_start)} ~ {_day(w.test_end):<10}  "
            f"{_params_text(w.params):<20}{score:>12}{w.test_return:>+11.2%}{w.benchmark_return:>+11.2%}"
            f"{w.test_trades:>7}")
    lines.append("")
    lines.append(f"同一段样本外区间（{_day(int(wf.equity.index[0]))} 起）的对比：")
    rows = [("walk-forward（滚动选参）", wf.metrics),
            (f"固定参数 {_params_text(wf.fixed_params)}", wf.fixed.metrics),
            (f"事后最优 {_params_text(wf.hindsight.params)} ⚠", wf.hindsight_result.metrics)]
    lines.append(f"{'':<36}{'总收益':>10}{'年化':>10}{'夏普':>8}{'最大回撤':>10}{'交易':>7}")
    for label, m in rows:
        sharpe = f"{m['sharpe']:.2f}" if math.isfinite(m["sharpe"]) else "-"
        lines.append(f"{label:<36}{m['total_return']:>+11.2%}{m['cagr']:>+11.2%}{sharpe:>9}"
                     f"{m['max_drawdown']:>+11.2%}{m['trades']:>8}")
    lines.append(f"{'买入持有':<36}{wf.metrics['benchmark_return']:>+11.2%}")
    lines.append("⚠ 事后最优 = 已知整段行情后挑出的最好参数，现实中拿不到，只用来衡量过拟合程度。")
    return "\n".join(lines)


def save_walkforward(wf, out_dir: str | Path, name: str, plot: bool = True) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []

    windows = pd.DataFrame([{
        "train_start": _day(w.train_start), "test_start": _day(w.test_start), "test_end": _day(w.test_end),
        "params": _params_text(w.params), "train_score": w.train_score, "train_return": w.train_return,
        "test_return": w.test_return, "benchmark_return": w.benchmark_return, "test_trades": w.test_trades,
    } for w in wf.windows])
    paths.append(out / f"{name}_wf_windows.csv")
    windows.to_csv(paths[-1], index=False)

    curves = pd.DataFrame({
        "walk_forward": wf.equity,
        "fixed_params": wf.fixed.equity.reindex(wf.equity.index),
        "hindsight_best": wf.hindsight_result.equity.reindex(wf.equity.index),
        "buy_and_hold": wf.benchmark,
    })
    curves.index = pd.to_datetime(curves.index, unit="ms", utc=True)
    paths.append(out / f"{name}_wf_equity.csv")
    curves.to_csv(paths[-1], index_label="time")

    if plot:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(11, 5))
        styles = {"walk_forward": dict(linewidth=1.6), "fixed_params": dict(linewidth=1),
                  "hindsight_best": dict(linewidth=1, linestyle="--"),
                  "buy_and_hold": dict(linewidth=1, alpha=0.6)}
        for col, style in styles.items():
            ax.plot(curves.index, curves[col], label=col.replace("_", " "), **style)
        for w in wf.windows[1:]:
            ax.axvline(pd.Timestamp(w.test_start, unit="ms", tz="UTC"), color="grey", alpha=0.2, linewidth=0.8)
        ax.set_title(f"{name} — out-of-sample (grey lines = re-optimisation)")
        ax.set_ylabel("equity")
        ax.grid(alpha=0.3)
        ax.legend()
        fig.tight_layout()
        paths.append(out / f"{name}_wf.png")
        fig.savefig(paths[-1], dpi=110)
        plt.close(fig)
    return paths
