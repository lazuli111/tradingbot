from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators import sma
from .base import Strategy


class MACross(Strategy):
    """Long while the fast SMA is above the slow SMA, short (perp only) while below."""

    name = "ma_cross"
    default_params = {"fast": 20, "slow": 50}
    param_grid = {"fast": [10, 20, 30, 50], "slow": [50, 100, 150, 200]}

    def validate(self) -> None:
        if not 1 <= self.params["fast"] < self.params["slow"]:
            raise ValueError("ma_cross 要求 1 <= fast < slow")

    @property
    def base_warmup(self) -> int:
        return self.params["slow"]

    def raw_signals(self, df: pd.DataFrame) -> pd.Series:
        fast = sma(df["close"], self.params["fast"])
        slow = sma(df["close"], self.params["slow"])
        sig = np.where(fast > slow, 1, np.where(fast < slow, -1, 0))
        sig[(fast.isna() | slow.isna()).to_numpy()] = 0
        return pd.Series(sig, index=df.index, dtype="int64")
