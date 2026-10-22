from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators import atr
from .base import Strategy


class VolSystem(Strategy):
    """Volatility System, after freqtrade-strategies `futures/VolatilitySystem`:
    go long when the close moved up by more than `atr_mult` x the previous bar's
    ATR over `change_bars` bars, short on the same move down, and reverse on the
    opposite signal. The original resamples 1h bars to 3h, uses ATR(14) x 2 and
    adds a second entry on a repeat signal; here the move is measured on the
    trading timeframe over `change_bars` bars and there is no pyramiding."""

    name = "vol_system"
    default_params = {"atr_period": 14, "atr_mult": 2.0, "change_bars": 1}
    param_grid = {"atr_mult": [1.0, 1.5, 2.0, 2.5, 3.0], "change_bars": [1, 3, 6]}

    def validate(self) -> None:
        p = self.params
        if p["atr_period"] < 2 or p["atr_mult"] <= 0 or p["change_bars"] < 1:
            raise ValueError("vol_system 要求 atr_period >= 2，atr_mult > 0，change_bars >= 1")

    @property
    def base_warmup(self) -> int:
        return max(self.params["atr_period"] + 1, self.params["change_bars"]) + 1

    def raw_signals(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        threshold = (p["atr_mult"] * atr(df, p["atr_period"]).shift(1)).to_numpy()
        change = df["close"].diff(p["change_bars"]).to_numpy()

        out = np.zeros(len(df), dtype="int64")
        pos = 0
        for i in range(len(df)):
            if np.isnan(threshold[i]) or np.isnan(change[i]):
                continue
            if change[i] > threshold[i]:
                pos = 1
            elif -change[i] > threshold[i]:
                pos = -1
            out[i] = pos
        return pd.Series(out, index=df.index)
