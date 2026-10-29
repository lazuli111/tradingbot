"""Portfolio combine: sleeves that start at different bars must line up by timestamp whatever
the --symbols order. pd.concat(axis=1) leaves an integer union index unsorted. No network."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import portfolio_backtest as pb  # noqa: E402

STEP = 4 * 3600 * 1000
T0 = 1_672_531_200_000


def sleeve(start_bar, n_bars, values):
    return pd.Series(np.asarray(values, dtype=float), index=T0 + (start_bar + np.arange(n_bars)) * STEP)


@pytest.fixture
def parts():
    """BTC trades from bar 0, SOL only lists at bar 4; both run to bar 9."""
    btc_eq = sleeve(0, 10, np.linspace(500, 600, 10))
    sol_eq = sleeve(4, 6, np.linspace(500, 450, 6))
    btc_px = sleeve(0, 10, np.linspace(100, 200, 10))
    sol_px = sleeve(4, 6, np.linspace(10, 30, 6))
    return {"BTC": btc_eq, "SOL": sol_eq}, {"BTC": btc_px, "SOL": sol_px}


def test_later_listed_symbol_first_matches_live_order(parts):
    equity, close = parts
    port, basket = pb.combine(equity, close, 1000)
    port_rev, basket_rev = pb.combine(dict(reversed(equity.items())), dict(reversed(close.items())), 1000)

    assert port.index.is_monotonic_increasing and basket.index.is_monotonic_increasing
    assert port.iloc[-1] == pytest.approx(600 + 450)   # last bar of each sleeve, not an early one
    pd.testing.assert_series_equal(port_rev, port)
    pd.testing.assert_series_equal(basket_rev, basket)


def test_portfolio_starts_once_every_sleeve_has_started(parts):
    equity, close = parts
    port, _ = pb.combine(dict(reversed(equity.items())), dict(reversed(close.items())), 1000)
    assert port.index[0] == T0 + 4 * STEP
    assert len(port) == 6
