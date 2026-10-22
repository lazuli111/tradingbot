"""Multi-symbol backtest on one shared account, with time-varying capital weights.

This mirrors how the live portfolio runs: every symbol has its own process, and each
process sizes a new entry against "account equity x its fraction" (FractionalEquityBroker).
Here the fraction may change every day (inverse-volatility weights). A weight change only
affects the size of the next entry; an open position is never resized, so reweighting
costs no extra orders.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..broker import SimBroker
from ..config import Config
from ..data import timeframe_ms
from ..indicators import atr as compute_atr
from ..metrics import MS_PER_YEAR, compute_metrics
from ..risk import RiskManager
from ..strategies import create_strategy
from ..trader import Trader
from .backtest import funding_per_bar

DAY_MS = 24 * 3600 * 1000


def daily_close(df: pd.DataFrame) -> pd.Series:
    """Last close of each UTC day, indexed by the day's start (ms)."""
    s = df.set_index("timestamp")["close"]
    return s.groupby(s.index // DAY_MS * DAY_MS).last()


def equal_weights(prices: pd.DataFrame) -> pd.DataFrame:
    """1/N over the symbols that have a price that day."""
    listed = prices.notna().astype(float)
    return listed.div(listed.sum(axis=1), axis=0).fillna(0.0)


def inverse_vol_weights(prices: pd.DataFrame, lookback: int) -> pd.DataFrame:
    """Risk-parity-lite weights from daily prices (index = day start ms, one column per symbol).

    Row d is the weight to use *during* day d, computed only from closes up to day d-1.
    w_i = (1/vol_i) / sum_j(1/vol_j), vol = std of the last `lookback` daily log returns.
    A symbol without `lookback` returns yet gets 0 while others can be measured; on days
    when no symbol can be measured, fall back to equal weight.
    """
    rets = np.log(prices).diff()
    vol = rets.rolling(lookback, min_periods=lookback).std()
    inv = (1.0 / vol).replace([np.inf, -np.inf], np.nan)
    w = inv.div(inv.sum(axis=1), axis=0).fillna(0.0)
    unmeasured = inv.isna().all(axis=1)
    w[unmeasured] = equal_weights(prices)[unmeasured]
    w = w.shift(1)
    w.iloc[0] = equal_weights(prices).iloc[0]
    return w


class _SharedAccount:
    """What one sleeve sees as "equity": the whole account, with the other sleeves at
    their last close and this one at the current price. The circuit breakers therefore
    act on account equity, the same percentages the live FractionalEquityBroker gives."""

    def __init__(self, inner: SimBroker, book: "_Book", sym: str):
        self._inner = inner
        self._book = book
        self._sym = sym

    def equity(self, price: float) -> float:
        return self._book.equity - self._book.marks[self._sym] + self._inner.equity(price)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _WeightedRisk(RiskManager):
    """Sizes a new entry against account equity x the sleeve's current weight."""

    def __init__(self, cfg, is_perp: bool, initial_equity: float, book: "_Book", sym: str):
        super().__init__(cfg, is_perp, initial_equity)
        self._book = book
        self._sym = sym

    def plan(self, side: int, equity: float, price: float, atr: float):
        return super().plan(side, equity * self._book.weight[self._sym], price, atr)


class _Book:
    """Shared account: each sleeve's broker holds the whole capital as margin, so the
    account equity is capital + the sum of every sleeve's PnL."""

    def __init__(self, capital: float, syms: list[str]):
        self.capital = capital
        self.marks = {sym: capital for sym in syms}   # each sleeve's equity at its last close
        self.weight = {sym: 0.0 for sym in syms}

    @property
    def equity(self) -> float:
        return self.capital + sum(m - self.capital for m in self.marks.values())


@dataclass
class PortfolioResult:
    equity: pd.Series          # account equity per bar (ms index)
    trades: list[dict]         # every closed trade, with a "symbol" key
    metrics: dict              # compute_metrics(...) plus "turnover" (traded notional / avg equity / year)
    per_symbol: dict[str, dict]
    weights: pd.DataFrame      # the daily target weights that were used


def run_portfolio(cfgs: dict[str, Config], data: dict[str, tuple[pd.DataFrame, pd.DataFrame | None]],
                  weights: pd.DataFrame, capital: float, start_ts: int | None = None) -> PortfolioResult:
    """`weights`: daily target weights (index = day start ms, columns = symbols), e.g. from
    `inverse_vol_weights`. On each bar they are renormalized over the symbols already trading."""
    syms = list(cfgs)
    book = _Book(capital, syms)
    sleeves = {}
    for sym in syms:
        cfg = cfgs[sym]
        df, funding = data[sym]
        strategy = create_strategy(cfg.strategy.name, cfg.strategy.params)
        ts = df["timestamp"].to_numpy()
        start = max(strategy.warmup, cfg.risk.atr_period)
        if start_ts is not None:
            start = max(start, int(np.searchsorted(ts, start_ts)))
        # the whole account backs every sleeve's margin; risk sizing keeps notional <= weight x equity
        inner = SimBroker(cfg.is_perp, capital, cfg.fee_rate, cfg.slippage,
                          cfg.risk.leverage if cfg.is_perp else 1.0, cfg.maintenance_margin_rate)
        trader = Trader(_WeightedRisk(cfg.risk, cfg.is_perp, capital, book, sym),
                        _SharedAccount(inner, book, sym), allow_short=cfg.is_perp)
        day = pd.Index(ts // DAY_MS * DAY_MS)
        w = weights[sym].reindex(day, method="ffill").fillna(0.0).to_numpy() if sym in weights else np.zeros(len(ts))
        sleeves[sym] = dict(
            ts=ts, start=start, pos={int(t): i for i, t in enumerate(ts)}, broker=inner, trader=trader,
            o=df["open"].to_numpy(float), h=df["high"].to_numpy(float), l=df["low"].to_numpy(float),
            c=df["close"].to_numpy(float), w=w,
            sig=strategy.signals(df).fillna(0).astype(int).to_numpy(),
            atr=compute_atr(df, cfg.risk.atr_period).to_numpy(),
            fund=funding_per_bar(df, funding if cfg.is_perp else None), cur=None)

    timeline = sorted({int(t) for s in sleeves.values() for t in s["ts"][s["start"]:]})
    if not timeline:
        raise ValueError("没有任何品种的数据覆盖预热期之后的区间")
    out = np.empty(len(timeline))
    for k, t in enumerate(timeline):
        active = []
        for sym, s in sleeves.items():
            i = s["pos"].get(t)
            if i is not None and i >= s["start"]:
                s["cur"] = i
                active.append(sym)
        # weights are renormalized over the sleeves that have started trading
        trading = {sym: s["w"][s["cur"]] for sym, s in sleeves.items() if s["cur"] is not None}
        total = sum(trading.values())
        for sym in syms:
            book.weight[sym] = trading[sym] / total if sym in trading and total > 0 else 0.0
        for sym in active:
            s = sleeves[sym]
            i = s["cur"]
            s["trader"].on_bar_open(t, s["o"][i], s["fund"][i])
            s["trader"].check_exits(t, s["o"][i], s["h"][i], s["l"][i])
            s["trader"].on_bar_close(t, s["c"][i], int(s["sig"][i]), s["atr"][i])
        for sym in active:  # after the whole bar, so no sleeve sees another's same-bar close
            s = sleeves[sym]
            book.marks[sym] = s["broker"].equity(s["c"][s["cur"]])
        out[k] = book.equity

    trades, per_symbol, fees, funding_paid = [], {}, 0.0, 0.0
    for sym, s in sleeves.items():
        b = s["broker"]
        if b.position is not None:  # mark the open trade as closed for the statistics
            b.close(s["c"][-1], int(s["ts"][-1]), "end_of_data")
        sym_trades = [{**tr, "symbol": sym} for tr in b.stats.closed_trades]
        trades += sym_trades
        fees += b.stats.fees
        funding_paid += b.stats.funding
        per_symbol[sym] = {"pnl": b.cash - capital, "trades": len(sym_trades), "fees": b.stats.fees,
                           "funding": b.stats.funding}
    out[-1] = capital + sum(s["broker"].cash - capital for s in sleeves.values())

    equity = pd.Series(out, index=pd.Index(timeline, name="timestamp"))
    bar_ms = timeframe_ms(next(iter(cfgs.values())).timeframe)
    metrics = compute_metrics(equity, trades, capital, bar_ms, fees=fees, funding=funding_paid)
    metrics["turnover"] = turnover(trades, equity, bar_ms)
    return PortfolioResult(equity, trades, metrics, per_symbol, weights)


def turnover(trades: list[dict], equity: pd.Series, bar_ms: int) -> float:
    """Traded notional (entries + exits) per year, as a multiple of average equity."""
    years = len(equity) * bar_ms / MS_PER_YEAR
    notional = sum(t["qty"] * (t["entry"] + t["exit"]) for t in trades)
    return notional / float(equity.mean()) / years if years > 0 else math.nan
