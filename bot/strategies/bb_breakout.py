from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators import bollinger, rolling_percentile
from .base import Strategy

# Ichimoku cloud with the classic settings jesse's SimpleBollinger uses
TENKAN, KIJUN, SENKOU_B, DISPLACEMENT = 9, 26, 52, 26
SQUEEZE_LOOKBACK = 120   # bars over which the band width is ranked
SQUEEZE_WINDOW = 10      # the squeeze must have happened within this many bars before the breakout


def ichimoku_cloud(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """(span A, span B) as plotted under the current bar, i.e. computed DISPLACEMENT bars ago."""
    def mid(n: int) -> pd.Series:
        return (df["high"].rolling(n).max() + df["low"].rolling(n).min()) / 2
    span_a = ((mid(TENKAN) + mid(KIJUN)) / 2).shift(DISPLACEMENT)
    span_b = mid(SENKOU_B).shift(DISPLACEMENT)
    return span_a, span_b


class BBBreakout(Strategy):
    """Bollinger band breakout, after jesse-ai/example-strategies `SimpleBollinger`:
    enter when the close leaves the band (bands on hl2) in the direction of the
    Ichimoku cloud, exit when it crosses back over the middle band. Mirrored for
    shorts (the original is long-only).

    `squeeze_pct` < 1 adds a volatility-contraction condition: within the last
    SQUEEZE_WINDOW bars the band width must have ranked in the bottom
    `squeeze_pct` of the last SQUEEZE_LOOKBACK bars (1.0 = off)."""

    name = "bb_breakout"
    default_params = {"length": 20, "num_std": 2.0, "cloud": 1, "squeeze_pct": 1.0}
    param_grid = {"length": [20, 40], "num_std": [2.0, 2.5], "cloud": [1, 0], "squeeze_pct": [1.0, 0.25, 0.1]}

    def validate(self) -> None:
        p = self.params
        if p["length"] < 2 or p["num_std"] <= 0:
            raise ValueError("bb_breakout 要求 length >= 2，num_std > 0")
        if p["cloud"] not in (0, 1) or not 0 < p["squeeze_pct"] <= 1:
            raise ValueError("bb_breakout 要求 cloud 为 0/1，0 < squeeze_pct <= 1")

    @property
    def base_warmup(self) -> int:
        p = self.params
        need = [p["length"]]
        if p["cloud"]:
            need.append(SENKOU_B + DISPLACEMENT)
        if p["squeeze_pct"] < 1:
            need.append(p["length"] + SQUEEZE_LOOKBACK + SQUEEZE_WINDOW)
        return max(need)

    def raw_signals(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        lower, mid, upper = bollinger((df["high"] + df["low"]) / 2, p["length"], p["num_std"])
        close = df["close"].to_numpy()
        lo, md, up = lower.to_numpy(), mid.to_numpy(), upper.to_numpy()

        long_ok = np.ones(len(df), dtype=bool)
        short_ok = np.ones(len(df), dtype=bool)
        if p["cloud"]:
            span_a, span_b = ichimoku_cloud(df)
            top = np.maximum(span_a, span_b).to_numpy()   # NaN until both spans exist
            bottom = np.minimum(span_a, span_b).to_numpy()
            long_ok &= close > top        # NaN during warm-up -> False
            short_ok &= close < bottom
        if p["squeeze_pct"] < 1:
            width_rank = rolling_percentile((upper - lower) / mid, SQUEEZE_LOOKBACK)
            squeezed = (width_rank.rolling(SQUEEZE_WINDOW).min().shift(1) <= p["squeeze_pct"]).to_numpy()
            long_ok &= squeezed
            short_ok &= squeezed

        out = np.zeros(len(df), dtype="int64")
        pos = 0
        for i in range(len(df)):
            if np.isnan(md[i]):
                continue
            if (pos == 1 and close[i] < md[i]) or (pos == -1 and close[i] > md[i]):
                pos = 0
            if pos == 0:
                if close[i] > up[i] and long_ok[i]:
                    pos = 1
                elif close[i] < lo[i] and short_ok[i]:
                    pos = -1
            out[i] = pos
        return pd.Series(out, index=df.index)
