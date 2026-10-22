from __future__ import annotations

import numpy as np
import pandas as pd

from .base import Strategy

HOUR_MS = 3600 * 1000
# anchor period per trading timeframe, as jesse's utils.anchor_timeframe maps them
ANCHOR_MS = {HOUR_MS: 4 * HOUR_MS, 4 * HOUR_MS: 24 * HOUR_MS}


def anchor_open(df: pd.DataFrame) -> np.ndarray:
    """Open of the anchor period (4h for 1h bars, the UTC day for 4h bars, else 6 bars)
    each bar belongs to: the open of that period's first bar, which is never in the future."""
    ts = df["timestamp"].to_numpy()
    bar = int(np.median(np.diff(ts))) if len(ts) > 1 else HOUR_MS
    period = ANCHOR_MS.get(bar, 6 * bar)
    key = pd.Series(ts // period, index=df.index)
    return df["open"].groupby(key).transform("first").to_numpy()


class DualThrust(Strategy):
    """Michael Chalek's Dual Thrust, after jesse-ai/example-strategies `DUAL_THRUST`:
    Range = max(HH - LC, HC - LL) over the previous `lookback` bars; go long when
    the close is above the anchor-period open + k * Range, short below open - k * Range,
    and reverse on the opposite trigger (stop-and-reverse, no exit in between)."""

    name = "dual_thrust"
    default_params = {"lookback": 21, "k": 0.7}
    param_grid = {"lookback": [6, 12, 21, 42], "k": [0.3, 0.5, 0.7, 1.0]}

    def validate(self) -> None:
        if self.params["lookback"] < 1 or self.params["k"] <= 0:
            raise ValueError("dual_thrust 要求 lookback >= 1，k > 0")

    @property
    def base_warmup(self) -> int:
        return self.params["lookback"] + 1

    def raw_signals(self, df: pd.DataFrame) -> pd.Series:
        n, k = self.params["lookback"], self.params["k"]
        # shift(1): the range comes from the bars *before* the current one
        hh = df["high"].rolling(n).max().shift(1)
        ll = df["low"].rolling(n).min().shift(1)
        hc = df["close"].rolling(n).max().shift(1)
        lc = df["close"].rolling(n).min().shift(1)
        rng = np.fmax(hh - lc, hc - ll).to_numpy()
        base = anchor_open(df)
        upper, lower = base + k * rng, base - k * rng
        close = df["close"].to_numpy()

        out = np.zeros(len(df), dtype="int64")
        pos = 0
        for i in range(len(df)):
            if np.isnan(rng[i]):
                continue
            if close[i] > upper[i]:
                pos = 1
            elif close[i] < lower[i]:
                pos = -1
            out[i] = pos
        return pd.Series(out, index=df.index)
