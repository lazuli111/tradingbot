import pytest

from bot.broker import SimBroker


def test_spot_round_trip_fees():
    b = SimBroker(is_perp=False, initial_equity=1000, fee_rate=0.001)
    b.open(1, 5, 100, ts=0, reason="t")
    assert b.cash == pytest.approx(1000 - 500 - 0.5)
    assert b.equity(110) == pytest.approx(499.5 + 550)
    fill = b.close(110, ts=1, reason="t")
    assert b.cash == pytest.approx(499.5 + 550 - 0.55)
    assert fill.pnl == pytest.approx(50 - 0.5 - 0.55)
    assert b.stats.fees == pytest.approx(1.05)


def test_spot_cannot_short_or_overspend():
    b = SimBroker(is_perp=False, initial_equity=1000, fee_rate=0.001)
    with pytest.raises(ValueError):
        b.open(-1, 1, 100, 0, "t")
    b.open(1, 100, 100, 0, "t")                 # asks for 10000 USDT worth
    assert b.position.qty * 100 * 1.001 == pytest.approx(1000)
    assert b.cash == pytest.approx(0, abs=1e-9)


def test_slippage_is_adverse():
    b = SimBroker(is_perp=True, initial_equity=1000, fee_rate=0, slippage=0.01)
    b.open(-1, 1, 100, 0, "t")
    assert b.position.entry == pytest.approx(99)   # sold lower
    b.close(100, 1, "t")
    assert b.cash == pytest.approx(1000 - 2)       # bought back higher at 101


def test_perp_short_pnl_and_margin():
    b = SimBroker(is_perp=True, initial_equity=1000, fee_rate=0.0005, leverage=2)
    b.open(-1, 10, 100, 0, "t")
    assert b.position.margin == pytest.approx(500)
    assert b.cash == pytest.approx(1000 - 0.5)       # only the fee leaves the wallet
    assert b.equity(90) == pytest.approx(999.5 + 100)
    fill = b.close(90, 1, "t")
    assert fill.pnl == pytest.approx(100 - 0.5 - 0.45)


@pytest.mark.parametrize("side,lev,expected", [(1, 2, 100 * (1 - 0.5 + 0.004)),
                                               (-1, 2, 100 * (1 + 0.5 - 0.004)),
                                               (1, 1, 100 * 0.004)])
def test_liquidation_price(side, lev, expected):
    b = SimBroker(is_perp=True, initial_equity=1000, fee_rate=0, leverage=lev)
    assert b.liquidation_price(side, 100) == pytest.approx(expected)


def test_liquidation_loses_margin():
    b = SimBroker(is_perp=True, initial_equity=1000, fee_rate=0, leverage=3)
    b.open(1, 30, 100, 0, "t")
    fill = b.liquidate(1)
    assert fill.reason == "liquidation"
    assert b.cash == pytest.approx(0)
    assert b.position is None


def test_funding_direction():
    long_ = SimBroker(is_perp=True, initial_equity=1000, fee_rate=0)
    long_.open(1, 5, 100, 0, "t")
    long_.apply_funding(0.0001, 100, 1)
    assert long_.cash == pytest.approx(1000 - 0.05)   # longs pay when rate > 0
    short = SimBroker(is_perp=True, initial_equity=1000, fee_rate=0)
    short.open(-1, 5, 100, 0, "t")
    short.apply_funding(0.0001, 100, 1)
    assert short.cash == pytest.approx(1000 + 0.05)
    assert short.close(100, 2, "t").pnl == pytest.approx(0.05)


def test_state_round_trip():
    b = SimBroker(is_perp=True, initial_equity=1000, fee_rate=0.001, leverage=2)
    b.open(1, 2, 100, 0, "t")
    b.position.stop = 95
    c = SimBroker(is_perp=True, initial_equity=0, fee_rate=0.001, leverage=2)
    c.restore(b.state())
    assert c.cash == b.cash and c.position == b.position
