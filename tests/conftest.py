import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HOUR = 3600 * 1000


def make_ohlcv(closes, start_ts=1_735_689_600_000, spread=0.002):
    closes = np.asarray(closes, dtype=float)
    opens = np.r_[closes[0], closes[:-1]]
    return pd.DataFrame({
        "timestamp": start_ts + np.arange(len(closes)) * HOUR,
        "open": opens,
        "high": np.maximum(opens, closes) * (1 + spread),
        "low": np.minimum(opens, closes) * (1 - spread),
        "close": closes,
        "volume": 1.0,
    })


@pytest.fixture
def trend_df():
    """Flat, then a steady rally, then a steady decline."""
    closes = np.r_[np.full(60, 100.0), np.linspace(100, 150, 100), np.linspace(150, 90, 120)]
    return make_ohlcv(closes)
