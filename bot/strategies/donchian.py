from __future__ import annotations

import numpy as np
import pandas as pd

from .base import Strategy


class Donchian(Strategy):
    """Turtle-style breakout: enter on a break of the `entry`-bar high/low,
    exit when price breaks the opposite `exit`-bar channel."""

    name = "donchian"
    default_params = {"entry": 20, "exit": 10}
    param_grid = {"entry": [20, 40, 55, 80], "exit": [10, 20, 30]}

    def validate(self) -> None:
        if not 1 <= self.params["exit"] <= self.params["entry"]:
            raise ValueError("donchian 要求 1 <= exit <= entry")

    @property
    def base_warmup(self) -> int:
        return self.params["entry"] + 1

    def raw_signals(self, df: pd.DataFrame) -> pd.Series:
        n, m = self.params["entry"], self.params["exit"]
        # shift(1): compare today's close with the channel of the *previous* bars
        upper = df["high"].rolling(n).max().shift(1).to_numpy()
        lower = df["low"].rolling(n).min().shift(1).to_numpy()
        exit_low = df["low"].rolling(m).min().shift(1).to_numpy()
        exit_high = df["high"].rolling(m).max().shift(1).to_numpy()
        close = df["close"].to_numpy()

        out = np.zeros(len(df), dtype="int64")
        pos = 0
        for i in range(len(df)):
            if np.isnan(upper[i]):
                continue
            if close[i] > upper[i]:
                pos = 1
            elif close[i] < lower[i]:
                pos = -1
            elif pos == 1 and close[i] < exit_low[i]:
                pos = 0
            elif pos == -1 and close[i] > exit_high[i]:
                pos = 0
            out[i] = pos
        return pd.Series(out, index=df.index)
