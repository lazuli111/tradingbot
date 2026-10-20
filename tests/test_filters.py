import numpy as np
import pandas as pd
import pytest

from bot.indicators import adx, rolling_percentile
from bot.strategies import create_strategy
from bot.strategies.base import gate_entries
from conftest import make_ohlcv


@pytest.fixture
def chop_df():
    rng = np.random.default_rng(1)
    return make_ohlcv(100 + rng.normal(0, 1, 300))


def test_adx_separates_trend_from_chop(trend_df, chop_df):
    assert adx(make_ohlcv(np.linspace(100, 200, 200))).iloc[-1] > 60
    assert adx(chop_df).iloc[-1] < 25


def test_rolling_percentile():
    pct = rolling_percentile(pd.Series([1, 2, 3, 4, 0.5, 5.0]), 3)
    assert pct.iloc[:2].isna().all()
    assert pct.iloc[2:].tolist() == pytest.approx([1, 1, 1 / 3, 1])


@pytest.mark.parametrize("raw,allowed,expected", [
    ([0, 1, 1, 1, 0], [0, 1, 0, 0, 0], [0, 1, 1, 1, 0]),   # filter turning off doesn't force an exit
    ([1, 1, 1, 1], [0, 0, 1, 0], [0, 0, 1, 1]),            # blocked entry happens later in the same direction
    ([1, -1, -1], [1, 0, 0], [1, 0, 0]),                   # flip blocked -> exit to flat, no reversal
    ([0, -1, 0, -1], [1, 1, 0, 0], [0, -1, 0, 0]),         # re-entry after an exit needs the filter again
])
def test_gate_entries(raw, allowed, expected):
    assert gate_entries(np.array(raw), np.array(allowed, dtype=bool)).tolist() == expected


def test_strict_adx_blocks_entries_in_chop(chop_df):
    unfiltered = create_strategy("ma_cross", {"fast": 5, "slow": 20}).signals(chop_df)
    filtered = create_strategy("ma_cross", {"fast": 5, "slow": 20, "adx_min": 40}).signals(chop_df)
    assert (unfiltered != 0).sum() > 100
    assert (filtered != 0).sum() < (unfiltered != 0).sum() / 3


def test_filtered_signals_are_subset_of_raw(trend_df):
    s = create_strategy("ma_cross", {"fast": 5, "slow": 20, "adx_min": 25, "vol_max_pct": 0.7,
                                     "vol_lookback": 50})
    raw, out = s.raw_signals(trend_df).to_numpy(), s.signals(trend_df).to_numpy()
    assert ((out == 0) | (out == raw)).all()


def test_warmup_and_description():
    plain = create_strategy("ma_cross", {"fast": 5, "slow": 20})
    assert plain.warmup == 20 and plain.describe_params() == {"fast": 5, "slow": 20}
    vol = create_strategy("ma_cross", {"fast": 5, "slow": 20, "vol_max_pct": 0.8, "vol_lookback": 100})
    assert vol.warmup == 114
    assert "vol_max_pct" in vol.describe_params() and "adx_min" not in vol.describe_params()


@pytest.mark.parametrize("params", [{"adx_min": 120}, {"vol_min_pct": 0.9, "vol_max_pct": 0.5},
                                    {"vol_lookback": 1}])
def test_bad_filter_params(params):
    with pytest.raises(ValueError):
        create_strategy("ma_cross", params)
