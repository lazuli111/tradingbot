import numpy as np
import pytest

from bot.strategies import create_strategy
from conftest import make_ohlcv


def test_ma_cross_follows_trend(trend_df):
    sig = create_strategy("ma_cross", {"fast": 5, "slow": 20}).signals(trend_df)
    assert (sig.iloc[:20] == 0).all()          # warm-up
    assert sig.iloc[150] == 1                  # in the rally
    assert sig.iloc[-1] == -1                  # in the decline


def test_donchian_breakout_and_exit():
    closes = np.r_[np.full(30, 100.0), np.linspace(100, 130, 20), np.linspace(130, 110, 15)]
    sig = create_strategy("donchian", {"entry": 20, "exit": 5}).signals(make_ohlcv(closes))
    assert sig.iloc[35] == 1                   # broke above the 20-bar high
    assert sig.iloc[-1] in (0, -1)             # exited on the pullback


@pytest.mark.parametrize("name,params", [
    ("ma_cross", {"fast": 5, "slow": 20}),
    ("donchian", {"entry": 20, "exit": 10}),
    ("ma_cross", {"fast": 5, "slow": 20, "adx_min": 20}),
    ("donchian", {"entry": 20, "exit": 10, "adx_min": 15, "vol_max_pct": 0.8, "vol_lookback": 50}),
])
def test_no_lookahead(trend_df, name, params):
    """Signals must not change when future bars are appended."""
    strategy = create_strategy(name, params)
    full = strategy.signals(trend_df)
    for cut in (80, 150, 220):
        partial = strategy.signals(trend_df.iloc[:cut])
        assert (partial.to_numpy() == full.iloc[:cut].to_numpy()).all()


def test_bad_params_rejected():
    with pytest.raises(ValueError):
        create_strategy("ma_cross", {"fast": 50, "slow": 20})
    with pytest.raises(ValueError):
        create_strategy("ma_cross", {"period": 3})
    with pytest.raises(ValueError):
        create_strategy("nope")
