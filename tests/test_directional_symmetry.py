from types import SimpleNamespace

import pytest

from app.market.scanner import CoinWMarketScanner
from app.models.enums import Direction, RegimeState
from app.models.market import Candle
from app.models.regime import RegimeResult
from app.regime.advanced import _ema_metrics, classify_details, features, TREND
from app.strategy.breakout_retest import BreakoutRetestStrategy


def _trend_candles(*, direction: str, n: int = 300, start: float = 150.0, step: float = 0.15):
    candles = []
    price = float(start)
    sign = 1.0 if direction == "long" else -1.0
    for index in range(n):
        open_price = price
        close_price = price + sign * step
        high = max(open_price, close_price) + step * 0.35
        low = min(open_price, close_price) - step * 0.35
        candles.append(Candle(index * 300_000, open_price, high, low, close_price, 1000 + index))
        price = close_price
    return candles


def _breakout_setup(direction: str):
    sign = 1.0 if direction == "long" else -1.0
    base = _trend_candles(direction=direction, n=254, start=70.0 if sign > 0 else 150.0, step=0.12)
    candles = list(base)
    price = candles[-1].close
    pullback = 0.30

    # Five-bar reset back toward EMA20, mirrored for LONG and SHORT.
    for index in range(5):
        open_price = price
        close_price = price - sign * pullback
        high = max(open_price, close_price) + 0.05
        low = min(open_price, close_price) - 0.05
        candles.append(Candle(len(candles) * 300_000, open_price, high, low, close_price, 1600 + index))
        price = close_price

    # Continuation candle resumes the dominant trend.
    open_price = price
    close_price = price + sign * pullback * 1.8
    high = max(open_price, close_price) + (0.08 if sign > 0 else 0.05)
    low = min(open_price, close_price) - (0.05 if sign > 0 else 0.08)
    candles.append(Candle(len(candles) * 300_000, open_price, high, low, close_price, 1700))
    return candles


def test_scanner_score_is_identical_for_equal_positive_and_negative_moves():
    positive = CoinWMarketScanner._score_row({
        "instrument": "AAAUSDT", "last": "10", "volume": "1000000",
        "openInterest": "10000000", "change24h": "0.04",
    })
    negative = CoinWMarketScanner._score_row({
        "instrument": "BBBUSDT", "last": "10", "volume": "1000000",
        "openInterest": "10000000", "change24h": "-0.04",
    })

    assert positive is not None and negative is not None
    assert positive["score"] == negative["score"]
    assert positive["momentum_score"] == negative["momentum_score"]
    assert positive["market_direction"] == "up"
    assert negative["market_direction"] == "down"


def test_ema_alignment_is_mirrored_for_bullish_and_bearish_trends():
    bullish_closes = [100.0 + index * 0.10 for index in range(300)]
    bearish_closes = [200.0 - index * 0.10 for index in range(300)]

    bull = _ema_metrics(bullish_closes[-1], bullish_closes)
    bear = _ema_metrics(bearish_closes[-1], bearish_closes)

    assert bull["ema_stack_alignment"] == pytest.approx(bear["ema_stack_alignment"])
    assert bull["ema_bullish_alignment"] == pytest.approx(bear["ema_bearish_alignment"])
    assert bull["ema_bearish_alignment"] == pytest.approx(bear["ema_bullish_alignment"])
    assert bull["ema_alignment_edge"] == pytest.approx(-bear["ema_alignment_edge"])
    assert bull["trend_bias"] == "long"
    assert bear["trend_bias"] == "short"


def test_bearish_market_features_classify_as_trend_with_short_bias():
    candles = _trend_candles(direction="short", n=300, start=200.0, step=0.15)
    market_features = features(candles, candles)
    result = classify_details(market_features)

    assert market_features["ema_bearish_alignment"] == pytest.approx(1.0)
    assert market_features["ema_bullish_alignment"] == pytest.approx(0.0)
    assert market_features["trend_bias"] == "short"
    assert result["candidate_regime"] == TREND
    assert result["regime_bias"] == "short"


@pytest.mark.parametrize(
    ("side", "expected"),
    (("long", Direction.LONG), ("short", Direction.SHORT)),
)
def test_breakout_retest_emits_the_correct_direction_for_mirrored_valid_setups(side, expected):
    candles_5m = _breakout_setup(side)
    candles_15m = _trend_candles(direction=side, n=300, start=50.0 if side == "long" else 200.0, step=0.15)
    candles_1h = _trend_candles(direction=side, n=300, start=30.0 if side == "long" else 300.0, step=0.20)
    regime_direction = Direction.BULLISH if side == "long" else Direction.BEARISH
    regime = RegimeResult(
        RegimeState.TRENDING,
        RegimeState.TRENDING,
        regime_direction,
        "HIGH",
        90,
        True,
        False,
        "BREAKOUT_RETEST",
        1.0,
        0,
        90,
        False,
        (),
    )
    snapshot = SimpleNamespace(
        timeframes={"5m": candles_5m, "15m": candles_15m, "1h": candles_1h}
    )

    strategy = BreakoutRetestStrategy()
    intent = strategy.evaluate(
        regime,
        candles_5m,
        f"decision-{side}",
        "BTC",
        "5m",
        candles_5m[-1].close,
        snapshot=snapshot,
    )

    assert intent is not None, strategy.last_trace
    assert intent.direction == expected
    if expected == Direction.LONG:
        assert intent.stop_price < intent.entry_price < intent.target_price
    else:
        assert intent.target_price < intent.entry_price < intent.stop_price


def test_scanner_audit_reports_directional_breadth_without_using_it_as_a_gate():
    class Audit:
        def __init__(self):
            self.events = []

        def event(self, name, correlation_id, **payload):
            self.events.append((name, correlation_id, payload))

    class Client:
        async def tickers(self):
            return {"data": [
                {"instrument": "AAAUSDT", "last": "10", "volume": "1000000", "openInterest": "10000000", "change24h": "0.03"},
                {"instrument": "BBBUSDT", "last": "10", "volume": "1000000", "openInterest": "10000000", "change24h": "-0.03"},
                {"instrument": "CCCUSDT", "last": "10", "volume": "900000", "openInterest": "10000000", "change24h": "0"},
            ]}

    import asyncio

    audit = Audit()
    rows = asyncio.run(CoinWMarketScanner(Client(), depth=3, cache_seconds=0, audit=audit).ranked())
    assert len(rows) == 3
    scan = [payload for name, _, payload in audit.events if name == "MARKET_SCAN_DONE"][-1]
    assert scan["ranking_mode"] == "direction_neutral_momentum"
    assert scan["eligible_breadth"] == {"up": 1, "down": 1, "flat": 1}
    assert scan["shortlist_breadth"] == {"up": 1, "down": 1, "flat": 1}
