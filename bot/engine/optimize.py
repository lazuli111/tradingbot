"""Parameter grid search and walk-forward (rolling out-of-sample) validation.

Walk-forward: pick the best parameters on a training window, trade them on the
following test window that the search never saw, roll forward by one test window
and repeat. Only the stitched test windows count — they show how the strategy
*plus its parameter-selection procedure* would have done in real time.
"""
from __future__ import annotations

import itertools
import logging
import math
from contextlib import contextmanager
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from ..config import Config, StrategyConfig
from ..data import timeframe_ms
from ..metrics import compute_metrics
from ..strategies import STRATEGIES
from .backtest import BacktestResult, run_backtest

DAY_MS = 24 * 3600 * 1000
OBJECTIVES = ("sharpe", "return", "calmar")


def param_combos(grid: dict[str, list]) -> list[dict]:
    keys = list(grid)
    return [dict(zip(keys, values)) for values in itertools.product(*(grid[k] for k in keys))]


def with_params(cfg: Config, params: dict) -> Config:
    """Config with `params` layered over the configured strategy params, so a grid
    over e.g. adx_min keeps the configured fast/slow (and vice versa)."""
    return replace(cfg, strategy=StrategyConfig(cfg.strategy.name, {**cfg.strategy.params, **params}))


def score(metrics: dict, objective: str, min_trades: int) -> float:
    if metrics["trades"] < min_trades:
        return -math.inf
    if objective == "sharpe":
        value = metrics["sharpe"]
    elif objective == "return":
        value = metrics["total_return"]
    elif objective == "calmar":
        dd = abs(metrics["max_drawdown"])
        value = metrics["cagr"] / dd if dd > 0 else math.inf
    else:
        raise ValueError(f"未知优化目标 {objective!r}，可选 {OBJECTIVES}")
    return value if value is not None and math.isfinite(value) else -math.inf


@contextmanager
def _quiet():
    """Silence per-run INFO/WARNING logs (circuit breaker etc.) during searches."""
    logger = logging.getLogger("bot")
    old = logger.level
    logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        logger.setLevel(old)


@dataclass
class Candidate:
    params: dict
    score: float
    metrics: dict


def grid_search(cfg: Config, df: pd.DataFrame, grid: dict[str, list],
                funding: pd.DataFrame | None = None, objective: str = "sharpe",
                min_trades: int = 5, start_ts: int | None = None) -> list[Candidate]:
    """Backtest every parameter combination; best first. Invalid combinations are skipped."""
    out = []
    with _quiet():
        for params in param_combos(grid):
            try:
                res = run_backtest(with_params(cfg, params), df, funding, start_ts)
            except ValueError:  # e.g. fast >= slow, or not enough bars for this warm-up
                continue
            out.append(Candidate(params, score(res.metrics, objective, min_trades), res.metrics))
    out.sort(key=lambda c: c.score, reverse=True)
    return out


def make_windows(first_ts: int, end_ts: int, train_ms: int, test_ms: int) -> list[tuple[int, int, int]]:
    """Rolling (train_start, test_start, test_end) triples; the last test window may be shorter."""
    windows = []
    t = first_ts
    while t + train_ms < end_ts:
        test_start = t + train_ms
        windows.append((t, test_start, min(test_start + test_ms, end_ts)))
        t += test_ms
    return windows


@dataclass
class WindowResult:
    train_start: int
    test_start: int
    test_end: int
    params: dict | None       # None: nothing qualified on the training window, stayed in cash
    train_score: float
    train_return: float | None
    test_return: float
    benchmark_return: float
    test_trades: int


@dataclass
class WalkForwardResult:
    windows: list[WindowResult]
    equity: pd.Series                    # stitched out-of-sample equity
    benchmark: pd.Series                 # buy & hold over the same span
    trades: list[dict]
    metrics: dict
    fixed: BacktestResult                # config's own params over the same span
    fixed_params: dict
    hindsight: Candidate                 # best params chosen *with* knowledge of the span
    hindsight_result: BacktestResult
    objective: str


def walk_forward(cfg: Config, df: pd.DataFrame, grid: dict[str, list] | None = None,
                 funding: pd.DataFrame | None = None, train_days: int = 180,
                 test_days: int = 60, objective: str = "sharpe",
                 min_trades: int = 5) -> WalkForwardResult:
    if objective not in OBJECTIVES:
        raise ValueError(f"未知优化目标 {objective!r}，可选 {OBJECTIVES}")
    grid = grid or STRATEGIES[cfg.strategy.name].param_grid
    if not grid:
        raise ValueError(f"策略 {cfg.strategy.name} 没有默认参数网格，请用 --grid 指定")
    unknown = set(grid) - STRATEGIES[cfg.strategy.name].param_names()
    if unknown:
        raise ValueError(f"策略 {cfg.strategy.name} 没有参数 {sorted(unknown)}，"
                         f"可用: {sorted(STRATEGIES[cfg.strategy.name].param_names())}")
    ts = df["timestamp"].to_numpy()
    close = df["close"].to_numpy(dtype=float)
    bar = timeframe_ms(cfg.timeframe)

    # bars of history each test window needs in front of it for indicator warm-up
    warmups = []
    for params in param_combos(grid):
        try:
            warmups.append(STRATEGIES[cfg.strategy.name](**{**cfg.strategy.params, **params}).warmup)
        except ValueError:
            pass
    if not warmups:
        raise ValueError("参数网格里没有任何合法组合")
    lookback = max(warmups) + cfg.risk.atr_period + 1

    windows = make_windows(int(ts[0]), int(ts[-1]) + bar, train_days * DAY_MS, test_days * DAY_MS)
    if not windows:
        raise ValueError(f"数据只覆盖 {(ts[-1] - ts[0]) / DAY_MS:.0f} 天，不够一个 {train_days} 天的训练窗口")

    capital = cfg.initial_equity
    equity_parts, trades, rows = [], [], []
    fees = funding_paid = 0.0
    for train_start, test_start, test_end in windows:
        i_test, i_end = np.searchsorted(ts, test_start), np.searchsorted(ts, test_end)
        if i_end - i_test < 2:
            continue
        train_df = df.iloc[np.searchsorted(ts, train_start):i_test]
        candidates = grid_search(cfg, train_df, grid, funding, objective, min_trades)
        best = candidates[0] if candidates and candidates[0].score > -math.inf else None

        start_capital = capital
        if best is None:
            eq = pd.Series(capital, index=pd.Index(ts[i_test:i_end], name="timestamp"), dtype=float)
            n_trades = 0
        else:
            test_df = df.iloc[max(0, i_test - lookback):i_end]
            with _quiet():
                res = run_backtest(with_params(cfg, best.params), test_df, funding, start_ts=test_start)
            scale = capital / cfg.initial_equity  # compound: each window starts with the last one's capital
            eq = res.equity * scale
            for t in res.trades:
                trades.append({**t, "pnl": t["pnl"] * scale, "fees": t["fees"] * scale,
                               "funding": t["funding"] * scale, "params": best.params})
            fees += res.metrics["fees"] * scale
            funding_paid += res.metrics["funding"] * scale
            n_trades = len(res.trades)
        capital = float(eq.iloc[-1])
        equity_parts.append(eq)
        rows.append(WindowResult(
            train_start, test_start, test_end, best.params if best else None,
            best.score if best else -math.inf, best.metrics["total_return"] if best else None,
            capital / start_capital - 1, close[i_end - 1] / close[i_test] - 1, n_trades))

    equity = pd.concat(equity_parts)
    first_test = int(equity.index[0])
    i_first = np.searchsorted(ts, first_test)
    close_at = pd.Series(close, index=ts).loc[equity.index]
    benchmark = cfg.initial_equity * close_at / close_at.iloc[0]
    metrics = compute_metrics(equity, trades, cfg.initial_equity, bar,
                              benchmark_return=float(benchmark.iloc[-1] / cfg.initial_equity - 1),
                              fees=fees, funding=funding_paid)

    # Two reference points over exactly the same out-of-sample span:
    span_df = df.iloc[max(0, i_first - lookback):]
    with _quiet():
        fixed = run_backtest(cfg, span_df, funding, start_ts=first_test)
    fixed_params = STRATEGIES[cfg.strategy.name](**cfg.strategy.params).describe_params()
    hindsight = grid_search(cfg, span_df, grid, funding, objective, min_trades, start_ts=first_test)[0]
    with _quiet():
        hindsight_result = run_backtest(with_params(cfg, hindsight.params), span_df, funding,
                                        start_ts=first_test)
    return WalkForwardResult(rows, equity, benchmark, trades, metrics, fixed, fixed_params,
                             hindsight, hindsight_result, objective)
