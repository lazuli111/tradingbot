"""Technical indicators. Every value at index i uses only data up to and including bar i."""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def wilder(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (an EMA with alpha = 1/period), as used by ATR and ADX."""
    return series.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range with Wilder smoothing."""
    return wilder(true_range(df), period)


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index (0-100): how strongly price is trending, in either direction.

    Common reading: < 20 no clear trend, > 25 trending.
    """
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = wilder(true_range(df), period)
    plus_di = 100 * wilder(plus_dm, period) / tr
    minus_di = 100 * wilder(minus_dm, period) / tr
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    return wilder(dx, period)


def rolling_percentile(series: pd.Series, lookback: int) -> pd.Series:
    """Percentile rank (0-1] of each value among the last `lookback` values, itself included."""
    return series.rolling(lookback, min_periods=lookback).rank(pct=True)


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's Relative Strength Index (0-100); 50 when the window had no price change."""
    delta = series.diff()
    gain = wilder(delta.clip(lower=0), period)
    loss = wilder(-delta.clip(upper=0), period)
    total = (gain + loss).replace(0, np.nan)
    return (100 * gain / total).where(total.notna() | gain.isna(), 50.0)


def bollinger(series: pd.Series, period: int = 20, num_std: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    """(lower, middle, upper) Bollinger Bands, population standard deviation."""
    mid = sma(series, period)
    dev = series.rolling(period, min_periods=period).std(ddof=0)
    return mid - num_std * dev, mid, mid + num_std * dev
