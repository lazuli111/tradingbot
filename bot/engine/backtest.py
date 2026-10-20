"""Bar-by-bar backtester: strategy -> risk -> SimBroker, with no look-ahead.

A signal computed on the close of bar i is executed at the open of bar i+1.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..broker import SimBroker
from ..config import Config
from ..data import timeframe_ms
from ..indicators import atr as compute_atr
from ..metrics import compute_metrics
from ..risk import RiskManager
from ..strategies import create_strategy
from ..trader import Trader


@dataclass
class BacktestResult:
    equity: pd.Series      # indexed by bar timestamp (ms)
    benchmark: pd.Series   # buy & hold of the same capital
    trades: list[dict]
    metrics: dict


def funding_per_bar(df: pd.DataFrame, funding: pd.DataFrame | None) -> np.ndarray:
    """Sum of funding rates whose timestamp falls inside each bar [open, next open)."""
    out = np.zeros(len(df))
    if funding is None or funding.empty:
        return out
    idx = np.searchsorted(df["timestamp"].to_numpy(), funding["timestamp"].to_numpy(), side="right") - 1
    valid = idx >= 0
    np.add.at(out, idx[valid], funding["rate"].to_numpy()[valid])
    return out


def run_backtest(cfg: Config, df: pd.DataFrame, funding: pd.DataFrame | None = None,
                 start_ts: int | None = None) -> BacktestResult:
    """Bars before `start_ts` only warm up the indicators; trading starts at `start_ts`."""
    strategy = create_strategy(cfg.strategy.name, cfg.strategy.params)
    signals = strategy.signals(df).fillna(0).astype(int).to_numpy()
    atr = compute_atr(df, cfg.risk.atr_period).to_numpy()
    funding_rates = funding_per_bar(df, funding if cfg.is_perp else None)

    broker = SimBroker(cfg.is_perp, cfg.initial_equity, cfg.fee_rate, cfg.slippage,
                       cfg.risk.leverage if cfg.is_perp else 1.0, cfg.maintenance_margin_rate)
    risk = RiskManager(cfg.risk, cfg.is_perp, cfg.initial_equity)
    trader = Trader(risk, broker, allow_short=cfg.is_perp)

    ts = df["timestamp"].to_numpy()
    start = max(strategy.warmup, cfg.risk.atr_period)
    if start_ts is not None:
        start = max(start, int(np.searchsorted(ts, start_ts)))
    if len(df) <= start + 1:
        raise ValueError(f"数据只有 {len(df)} 根 K 线，不足以覆盖预热期 / 起始位置 {start}")

    o, h, l, c = (df[col].to_numpy(dtype=float) for col in ("open", "high", "low", "close"))
    equity = np.empty(len(df) - start)
    for k, i in enumerate(range(start, len(df))):
        trader.on_bar_open(int(ts[i]), o[i], funding_rates[i])
        trader.check_exits(int(ts[i]), o[i], h[i], l[i])
        equity[k] = trader.on_bar_close(int(ts[i]), c[i], int(signals[i]), atr[i])

    if broker.position is not None:  # mark the open trade as closed for the statistics
        broker.close(c[-1], int(ts[-1]), "end_of_data")
        equity[-1] = broker.equity(c[-1])

    index = pd.Index(ts[start:], name="timestamp")
    equity_s = pd.Series(equity, index=index)
    benchmark = pd.Series(cfg.initial_equity * c[start:] / c[start], index=index)
    metrics = compute_metrics(
        equity_s, broker.stats.closed_trades, cfg.initial_equity, timeframe_ms(cfg.timeframe),
        benchmark_return=float(benchmark.iloc[-1] / cfg.initial_equity - 1),
        fees=broker.stats.fees, funding=broker.stats.funding)
    return BacktestResult(equity_s, benchmark, broker.stats.closed_trades, metrics)
