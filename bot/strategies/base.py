"""Strategy interface.

A strategy only decides the *direction* it wants to hold (LONG / SHORT / FLAT).
Position size, stops and execution are handled by RiskManager and Broker, so the
same strategy runs unchanged in backtest, paper trading and on an exchange.

Every strategy also accepts optional entry filters (off by default). They only
gate *new* entries; a position that is already open is held until the strategy's
own exit rule fires, so a filter flickering on and off doesn't churn trades.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from enum import IntEnum

import numpy as np
import pandas as pd

from ..indicators import adx, atr, rolling_percentile

VOL_ATR_PERIOD = 14

FILTER_DEFAULTS = {
    "adx_period": 14,
    "adx_min": 0,          # trend strength: enter only when ADX >= adx_min (0 = off)
    "vol_lookback": 180,   # bars over which ATR/price is ranked
    "vol_min_pct": 0.0,    # enter only when the volatility percentile is within
    "vol_max_pct": 1.0,    # [vol_min_pct, vol_max_pct] (0 / 1 = off)
}


class Signal(IntEnum):
    SHORT = -1
    FLAT = 0
    LONG = 1


def gate_entries(raw: np.ndarray, allowed: np.ndarray) -> np.ndarray:
    """Apply an entry filter to a raw direction series.

    Holding the current direction and going flat are always allowed; switching
    into a new long/short direction only happens on bars where `allowed` is true.
    If an entry is blocked the strategy stays flat and may enter later in the same
    direction once the filter passes.
    """
    out = np.zeros(len(raw), dtype="int64")
    prev = 0
    for i, want in enumerate(raw):
        if want == 0 or want == prev or allowed[i]:
            prev = int(want)
        else:
            prev = 0
        out[i] = prev
    return out


class Strategy(ABC):
    name: str = "base"
    default_params: dict = {}
    param_grid: dict[str, list] = {}  # default search space for walk-forward optimisation

    @classmethod
    def param_names(cls) -> set[str]:
        return set(cls.default_params) | set(FILTER_DEFAULTS)

    def __init__(self, **params):
        unknown = set(params) - self.param_names()
        if unknown:
            raise ValueError(f"策略 {self.name} 不认识参数 {sorted(unknown)}，"
                             f"可用参数: {sorted(self.param_names())}")
        self.params = {**self.default_params, **FILTER_DEFAULTS, **params}
        self._validate_filters()
        self.validate()

    def _validate_filters(self) -> None:
        p = self.params
        if p["adx_period"] < 2 or p["vol_lookback"] < 2:
            raise ValueError("adx_period / vol_lookback 至少为 2")
        if not 0 <= p["adx_min"] < 100:
            raise ValueError("adx_min 应在 [0, 100) 之间")
        if not 0 <= p["vol_min_pct"] < p["vol_max_pct"] <= 1:
            raise ValueError("要求 0 <= vol_min_pct < vol_max_pct <= 1")

    def validate(self) -> None:
        pass

    @property
    def adx_filter_on(self) -> bool:
        return self.params["adx_min"] > 0

    @property
    def vol_filter_on(self) -> bool:
        return self.params["vol_min_pct"] > 0 or self.params["vol_max_pct"] < 1

    @property
    def warmup(self) -> int:
        """Number of bars needed before signals are meaningful."""
        need = [self.base_warmup]
        if self.adx_filter_on:
            need.append(2 * self.params["adx_period"])
        if self.vol_filter_on:
            need.append(self.params["vol_lookback"] + VOL_ATR_PERIOD)
        return max(need)

    @property
    @abstractmethod
    def base_warmup(self) -> int:
        """Bars the strategy's own indicators need."""

    @abstractmethod
    def raw_signals(self, df: pd.DataFrame) -> pd.Series:
        """Unfiltered target direction per bar (-1/0/1), aligned with df.

        The value at row i may only use data from rows <= i.
        """

    def entry_allowed(self, df: pd.DataFrame) -> np.ndarray:
        """Bars on which the entry filters let a new position open."""
        allowed = np.ones(len(df), dtype=bool)
        p = self.params
        if self.adx_filter_on:
            strength = adx(df, p["adx_period"]).to_numpy()
            allowed &= strength >= p["adx_min"]          # NaN during warm-up -> False
        if self.vol_filter_on:
            vol = atr(df, VOL_ATR_PERIOD) / df["close"]
            pct = rolling_percentile(vol, p["vol_lookback"]).to_numpy()
            allowed &= (pct >= p["vol_min_pct"]) & (pct <= p["vol_max_pct"])
        return allowed

    def signals(self, df: pd.DataFrame) -> pd.Series:
        """Filtered target direction per bar; the value at row i is acted on at the open of bar i+1."""
        raw = self.raw_signals(df).fillna(0).astype("int64").to_numpy()
        if not (self.adx_filter_on or self.vol_filter_on):
            return pd.Series(raw, index=df.index)
        return pd.Series(gate_entries(raw, self.entry_allowed(df)), index=df.index)

    def describe_params(self) -> dict:
        """Strategy parameters plus only the filters that are switched on."""
        p = self.params
        out = {k: p[k] for k in self.default_params}
        if self.adx_filter_on:
            out.update(adx_period=p["adx_period"], adx_min=p["adx_min"])
        if self.vol_filter_on:
            out.update(vol_lookback=p["vol_lookback"], vol_min_pct=p["vol_min_pct"],
                       vol_max_pct=p["vol_max_pct"])
        return out

    def __repr__(self) -> str:
        args = ", ".join(f"{k}={v}" for k, v in self.describe_params().items())
        return f"{self.name}({args})"
