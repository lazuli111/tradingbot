import pytest

from bot.config import RiskConfig
from bot.risk import RiskManager

DAY = 24 * 3600 * 1000


def test_size_by_risk_and_cap():
    rm = RiskManager(RiskConfig(risk_per_trade=0.01, stop_atr_mult=2), is_perp=False, initial_equity=10_000)
    plan = rm.plan(1, 10_000, price=100, atr=5)
    assert plan.stop_distance == 10
    assert plan.qty == pytest.approx(10)          # 100 USDT risk / 10 stop distance
    tight = rm.plan(1, 10_000, price=100, atr=0.01)
    assert tight.qty == pytest.approx(100)        # capped at 100% of equity for spot


def test_perp_cap_uses_leverage():
    rm = RiskManager(RiskConfig(leverage=3, stop_atr_mult=1), is_perp=True, initial_equity=1000)
    assert rm.plan(-1, 1000, price=100, atr=0.001).qty == pytest.approx(30)


def test_invalid_atr_gives_no_plan():
    rm = RiskManager(RiskConfig(), is_perp=False, initial_equity=1000)
    assert rm.plan(1, 1000, 100, float("nan")) is None
    assert rm.plan(1, 1000, 100, 0) is None


def test_daily_loss_blocks_until_next_day():
    rm = RiskManager(RiskConfig(daily_loss_limit=0.03), is_perp=False, initial_equity=1000)
    rm.update(0, 1000)
    rm.update(3600_000, 960)
    assert rm.can_open(960)[0] is False
    rm.update(DAY, 960)                            # new UTC day resets the baseline
    assert rm.can_open(960)[0] is True


def test_max_drawdown_halts_for_good():
    rm = RiskManager(RiskConfig(max_drawdown=0.2), is_perp=False, initial_equity=1000)
    rm.update(0, 1500)
    rm.update(DAY, 1190)                           # -20.7% from the 1500 peak
    assert rm.halted
    rm.update(2 * DAY, 2000)
    assert rm.can_open(2000)[0] is False
