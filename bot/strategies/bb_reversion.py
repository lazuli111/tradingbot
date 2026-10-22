from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators import bollinger
from .base import Strategy


class BBReversion(Strategy):
    """Bollinger %B fade, after hummingbot `controllers/directional_trading/bollinger_v1`:
    go long when the close is below the lower band (%B < 0), short above the upper
    band (%B > 1), no trend filter. hummingbot exits through its executor's
    stop / take-profit / time barriers; here the position is closed once %B
    reverts to `exit_pctb` (long) or 1 - `exit_pctb` (short), with the usual ATR stop."""

    name = "bb_reversion"
    default_params = {"length": 100, "num_std": 2.0, "exit_pctb": 0.5}
    param_grid = {"length": [20, 50, 100], "num_std": [1.5, 2.0, 2.5], "exit_pctb": [0.5, 0.8]}

    def validate(self) -> None:
        p = self.params
        if p["length"] < 2 or p["num_std"] <= 0:
            raise ValueError("bb_reversion 要求 length >= 2，num_std > 0")
        if not 0.5 <= p["exit_pctb"] <= 1:
            raise ValueError("bb_reversion 要求 0.5 <= exit_pctb <= 1")

    @property
    def base_warmup(self) -> int:
        return self.params["length"]

    def raw_signals(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        lower, _, upper = bollinger(df["close"], p["length"], p["num_std"])
        width = (upper - lower).replace(0, np.nan)
        pctb = ((df["close"] - lower) / width).to_numpy()
        exit_long, exit_short = p["exit_pctb"], 1 - p["exit_pctb"]

        out = np.zeros(len(df), dtype="int64")
        pos = 0
        for i in range(len(df)):
            b = pctb[i]
            if np.isnan(b):
                out[i] = pos
                continue
            if (pos == 1 and b >= exit_long) or (pos == -1 and b <= exit_short):
                pos = 0
            if pos == 0:
                if b < 0:
                    pos = 1
                elif b > 1:
                    pos = -1
            out[i] = pos
        return pd.Series(out, index=df.index)
