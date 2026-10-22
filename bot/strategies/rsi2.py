from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators import rsi, sma
from .base import Strategy


class RSI2(Strategy):
    """Connors' RSI(2) pullback, after jesse-ai/example-strategies `RSI2`:
    in an uptrend (close above the `trend`-bar SMA) buy when a short RSI is
    oversold, sell once the close is back above the `exit`-bar SMA; mirrored for
    shorts in a downtrend. Short-term mean reversion inside the long-term trend."""

    name = "rsi2"
    default_params = {"rsi_period": 2, "oversold": 10, "trend": 200, "exit": 5}
    param_grid = {"rsi_period": [2, 3], "oversold": [5, 10, 20], "trend": [100, 200], "exit": [5, 10]}

    def validate(self) -> None:
        p = self.params
        if p["rsi_period"] < 2 or not 1 <= p["exit"] < p["trend"]:
            raise ValueError("rsi2 要求 rsi_period >= 2，1 <= exit < trend")
        if not 0 < p["oversold"] < 50:
            raise ValueError("rsi2 要求 0 < oversold < 50（超买阈值取 100 - oversold）")

    @property
    def base_warmup(self) -> int:
        return max(self.params["trend"], self.params["rsi_period"] + 1)

    def raw_signals(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        close = df["close"]
        r = rsi(close, p["rsi_period"]).to_numpy()
        trend = sma(close, p["trend"]).to_numpy()
        exit_ma = sma(close, p["exit"]).to_numpy()
        c = close.to_numpy()
        oversold, overbought = p["oversold"], 100 - p["oversold"]

        out = np.zeros(len(df), dtype="int64")
        pos = 0
        for i in range(len(df)):
            if np.isnan(trend[i]) or np.isnan(r[i]):
                continue
            if (pos == 1 and c[i] > exit_ma[i]) or (pos == -1 and c[i] < exit_ma[i]):
                pos = 0
            if pos == 0:
                if c[i] > trend[i] and r[i] <= oversold:
                    pos = 1
                elif c[i] < trend[i] and r[i] >= overbought:
                    pos = -1
            out[i] = pos
        return pd.Series(out, index=df.index)
