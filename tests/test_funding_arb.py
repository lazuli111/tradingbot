import numpy as np
import pandas as pd
import pytest

from bot import funding_arb as fa

HOUR = 3600 * 1000
T0 = 1_735_689_600_000  # 2025-01-01 00:00 UTC
FREE = fa.Costs(spot_fee=0.0, perp_fee=0.0, slippage=0.0, rebalance_band=10.0)


def make_events(rates, spot=100.0, perp=None, interval_h=8):
    n = len(rates)
    spot = np.broadcast_to(np.asarray(spot, float), (n,))
    perp = spot if perp is None else np.broadcast_to(np.asarray(perp, float), (n,))
    hourly = pd.DataFrame({"timestamp": T0 + np.arange(n) * interval_h * HOUR, "rate": rates,
                           "spot": spot, "perp": perp})
    hourly["basis"] = hourly["perp"] / hourly["spot"] - 1
    return fa.funding_events(hourly)


def test_positive_rate_is_income_for_the_short_leg():
    ev = make_events([0.0001] * 10)
    res = fa.simulate(ev, fa.always_hold(ev), FREE)
    # opened at event 0, so events 1..9 pay; nothing is collected on the entry event
    assert res["funding_pos"] == pytest.approx(9 * 0.0001)
    assert res["funding_neg"] == 0
    assert res["net"] == pytest.approx(9 * 0.0001)


def test_negative_rate_is_a_holding_cost():
    ev = make_events([-0.0002] * 5)
    res = fa.simulate(ev, fa.always_hold(ev), FREE)
    assert res["funding_pos"] == 0
    assert res["funding_neg"] == pytest.approx(-4 * 0.0002)
    assert res["net"] == pytest.approx(-4 * 0.0002)


def test_funding_paid_on_perp_notional():
    # 1 unit of spot notional, perp trades 1% rich: the payment is on the perp's value
    ev = make_events([0.001] * 2, spot=100.0, perp=101.0)
    res = fa.simulate(ev, fa.always_hold(ev), FREE)
    assert res["funding_pos"] == pytest.approx(0.001 * 1.01)


def test_round_trip_fees_and_slippage_are_deducted():
    ev = make_events([0.0] * 6)
    costs = fa.Costs(spot_fee=0.001, perp_fee=0.0005, slippage=0.0002, rebalance_band=10.0)
    res = fa.simulate(ev, fa.always_hold(ev), costs)
    per_side = 0.001 + 0.0005 + 2 * 0.0002
    assert res["fees"] == pytest.approx(2 * per_side)
    assert res["net"] == pytest.approx(-2 * per_side)
    assert res["trades"] == 1


def test_each_reentry_pays_another_round_trip():
    ev = make_events([0.0] * 6)
    costs = fa.Costs(spot_fee=0.001, perp_fee=0.0005, rebalance_band=10.0)
    hold = np.array([True, False, True, False, True, False])
    res = fa.simulate(ev, hold, costs)
    assert res["trades"] == 3
    assert res["fees"] == pytest.approx(3 * 2 * 0.0015)


def test_basis_narrowing_is_a_gain_and_widening_a_loss():
    narrowing = make_events([0.0] * 3, spot=100.0, perp=[100.5, 100.3, 100.2])
    res = fa.simulate(narrowing, fa.always_hold(narrowing), FREE)
    assert res["basis"] == pytest.approx(0.003)

    widening = make_events([0.0] * 3, spot=100.0, perp=[100.0, 100.4, 101.0])
    res = fa.simulate(widening, fa.always_hold(widening), FREE)
    assert res["basis"] == pytest.approx(-0.01)


def test_price_moves_with_constant_basis_cancel_out():
    px = [100.0, 120.0, 90.0, 150.0]
    ev = make_events([0.0] * 4, spot=px, perp=px)
    res = fa.simulate(ev, fa.always_hold(ev), FREE)
    assert res["basis"] == pytest.approx(0.0)
    assert res["net"] == pytest.approx(0.0)


def test_rebalancing_back_to_unit_notional_costs_fees():
    px = [100.0, 115.0, 115.0]
    ev = make_events([0.0] * 3, spot=px, perp=px)
    costs = fa.Costs(spot_fee=0.001, perp_fee=0.0005, rebalance_band=0.1)
    res = fa.simulate(ev, fa.always_hold(ev), costs)
    # notional drifted to 1.15 > 1.1 -> trade 0.15 back on both legs at event 1
    assert res["rebalance_fees"] == pytest.approx(0.15 * 0.0015)
    assert res["fees"] == pytest.approx(0.0015 + 1.0 * 0.0015)


def test_threshold_rule_uses_only_settled_rates():
    rates = [0.0001] * 5 + [0.001] * 5
    ev = make_events(rates)
    hold = fa.threshold_hold(ev, lookback=3, threshold=0.5)  # 0.001 per 8h ~ 110% APR
    assert not hold[:5].any()
    # event 5 is the first high rate; a 3-period mean crosses 50% APR once two of three are high
    assert list(hold[5:]) == [False, True, True, True, True]
    # changing the future never changes earlier decisions
    ev2 = make_events(rates[:7] + [-0.01] * 3)
    assert (fa.threshold_hold(ev2, 3, 0.5)[:7] == hold[:7]).all()


def test_apr_respects_funding_interval():
    ev8 = make_events([0.0001] * 3, interval_h=8)
    ev4 = make_events([0.0001] * 3, interval_h=4)
    assert ev8["apr"].iloc[-1] == pytest.approx(0.0001 * 3 * 365)
    assert ev4["apr"].iloc[-1] == pytest.approx(2 * ev8["apr"].iloc[-1])


def test_align_hourly_snaps_funding_to_the_hour():
    ts = T0 + np.arange(10) * HOUR
    bars = pd.DataFrame({"timestamp": ts, "open": 100.0, "high": 101.0, "low": 99.0,
                         "close": 100.0, "volume": 1.0})
    perp = bars.assign(open=100.5)
    funding = pd.DataFrame({"timestamp": [T0 + 8 * HOUR + 3], "rate": [0.0001]})
    hourly = fa.align_hourly(funding, bars, perp)
    assert hourly["rate"].notna().sum() == 1
    assert hourly.loc[hourly["rate"].notna(), "timestamp"].iloc[0] == T0 + 8 * HOUR
    assert hourly["basis"].iloc[0] == pytest.approx(0.005)


def test_walk_forward_chooses_params_from_the_past_only():
    rng = np.random.default_rng(0)
    rates = rng.normal(0.0001, 0.0002, 3 * 600)
    ev = make_events(rates)
    grid = fa.Grid(lookbacks=(3, 21), thresholds=(0.0, 0.1))
    costs = fa.Costs()
    wf = fa.walk_forward(ev, costs, grid, train_days=60, test_days=30)
    # scramble the second half: windows that start before it must pick the same params
    rates2 = rates.copy()
    rates2[900:] = -rates2[900:]
    wf2 = fa.walk_forward(make_events(rates2), costs, grid, train_days=60, test_days=30)
    cut = fa.fmt_ts(ev["timestamp"].iloc[900] - 60 * fa.DAY_MS)
    early = [w for w in wf["windows"] if w["test_start"] <= cut]
    assert early
    assert early == wf2["windows"][:len(early)]
    assert wf["events"]["timestamp"].iloc[0] >= ev["timestamp"].iloc[0] + 60 * fa.DAY_MS


def test_risk_metrics_counts_short_liquidations():
    n = 24 * 14
    opens = np.full(n, 100.0)
    highs = np.full(n, 100.0)
    highs[30] = 140.0  # +40% spike in the second day, first week
    hourly = pd.DataFrame({"timestamp": T0 + np.arange(n) * HOUR, "spot": opens, "perp": opens,
                           "perp_high": highs, "rate": np.nan})
    hourly["basis"] = hourly["perp"] / hourly["spot"] - 1
    risk = fa.risk_metrics(hourly, leverages=(1, 2, 3), mmr=0.004)
    assert risk["runup_7d"] == pytest.approx(0.40)
    assert (risk["liq_1x_7d"], risk["liq_2x_7d"], risk["liq_3x_7d"]) == (0, 0, 1)
    assert risk["liq_distance_3x"] == pytest.approx(1 / 3 - 0.004)


def test_max_basis_widening():
    basis_px = np.array([100.5, 100.1, 100.9, 100.3])
    hourly = pd.DataFrame({"timestamp": T0 + np.arange(4) * HOUR, "spot": 100.0, "perp": basis_px,
                           "perp_high": basis_px, "rate": np.nan})
    hourly["basis"] = hourly["perp"] / hourly["spot"] - 1
    assert fa.risk_metrics(hourly)["max_basis_widening"] == pytest.approx(0.008)


def test_yearly_funding_apr_groups_by_calendar_year():
    ev = make_events([0.0001] * 6)
    ev.loc[3:, "timestamp"] += 400 * fa.DAY_MS
    ev.loc[3:, "apr"] *= 2
    by_year = fa.yearly_funding_apr(ev)
    assert by_year == pytest.approx({2025: 0.0001 * 1095, 2026: 0.0002 * 1095})
