from app.models.enums import Direction, RegimeState
from app.models.regime import RegimeResult
from app.models.market import Candle
from app.risk.manager import RiskManager
from app.strategy.breakout_retest import BreakoutRetestStrategy


def test_risk_returns_quote_notional_for_coinw():
    intent = type("Intent", (), {
        "quality": 80,
        "entry_price": 100.0,
        "stop_price": 95.0,
        "risk_multiplier": 1.0,
    })()
    r = RiskManager(.01, 5).evaluate(intent, equity=1000, leverage=5)
    assert r.approved
    assert 4980 < r.quantity < 5000
    assert r.base_quantity == r.quantity / 100
    assert r.margin_required + r.quantity * .0006 <= 1000


def test_breakout_strategy_rejects_bearish_setup_when_history_is_insufficient():
    candles = []
    price = 100.0
    for i in range(60):
        o = price
        c = price - 0.6
        h = max(o, c) + 0.15
        l = min(o, c) - 0.25
        candles.append(Candle(i * 300000, o, h, l, c, 100))
        price = c

    regime = RegimeResult(
        RegimeState.TRENDING, RegimeState.TRENDING, Direction.BEARISH,
        "CLEAN", 80, True, True, "BREAKOUT_RETEST", 1.0, 10, 80
    )
    result = BreakoutRetestStrategy().evaluate(
        regime, candles, "D1", "BTC", "5m", candles[-1].close
    )
    assert result is None
