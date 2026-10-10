"""Regression: V2 breakout cannot execute on fast quote alone.

These tests model the entry failure class seen in AERO and BB. They do not
claim the missing AERO opening logs prove which confirmation was used.
"""
from types import SimpleNamespace
import time

import pytest

from app.models.enums import Direction, Strategy
from app.models.market import Candle
from app.models.trading import ArmedSetup
from app.strategy.entry_engine_v2 import EntryLifecycleV2


def _setup(now, direction):
    long = direction == Direction.LONG
    return ArmedSetup(
        setup_id=f"breakout-1d-{direction.value}", symbol="TEST",
        strategy=Strategy.BREAKOUT_RETEST, direction=direction,
        armed_at_ms=now-90_000, expires_at_ms=now+300_000,
        trigger_price=100.0, invalidation_price=98 if long else 102,
        stop_price=99 if long else 101,
        target_price=102 if long else 98,
        entry_zone_low=99.7, entry_zone_high=100.3,
        quality=89.0, risk_multiplier=1.0, timeframe="5m",
        metadata={"atr_value": 1.0, "minimum_viable_rr": .75},
    )


def _snap(now, direction, candle=None, *, in_progress=None):
    long = direction == Direction.LONG
    mid = 100.04 if long else 99.96
    candles = [candle] if candle is not None else []
    if in_progress is not None:
        candles.append(in_progress)
    return SimpleNamespace(
        bid=mid - .01, ask=mid + .01,
        bids=[(mid - .01, 60)] * 12, asks=[(mid + .01, 40)] * 12,
        orderbook_valid=True, quote_received_ms=now - 50,
        timeframes={"1m": candles},
    )


@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_breakout_never_triggers_without_confirmed_1m(monkeypatch, direction):
    now = int(time.time()*1000)
    monkeypatch.setattr("app.strategy.entry_engine_v2.time.time", lambda: now/1000)
    setup = _setup(now, direction)
    engine = EntryLifecycleV2(fast_confirm_enabled=True)
    status, intent, trace = engine.trigger(setup, _snap(now, direction), "no-candle")
    assert (status, intent) == ("pending", None)
    assert trace["reason"] == "waiting_closed_1m_confirmation"
    assert engine.consumed_record(setup.setup_id) is None


@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_breakout_weak_candle_waits_then_confirms_without_changing_exits(monkeypatch, direction):
    now = int(time.time()*1000)
    monkeypatch.setattr("app.strategy.entry_engine_v2.time.time", lambda: now/1000)
    setup = _setup(now, direction)
    engine = EntryLifecycleV2()
    if direction == Direction.LONG:
        weak = Candle(now - 60_000, 100.10, 100.12, 99.85, 99.90, 100)
        good = Candle(now - 60_000, 99.92, 100.10, 99.90, 100.07, 120)
    else:
        weak = Candle(now - 60_000, 99.90, 100.15, 99.87, 100.10, 100)
        good = Candle(now - 60_000, 100.08, 100.12, 99.90, 99.93, 120)
    status, intent, trace = engine.trigger(setup, _snap(now, direction, weak), "weak")
    assert status == "pending" and intent is None, trace
    assert trace["reason"] == "micro_confirmation_pending"
    assert engine.consumed_record(setup.setup_id) is None
    status, intent, trace = engine.trigger(setup, _snap(now, direction, good), "good")
    assert status == "triggered" and intent is not None, trace
    assert trace["confirmation_mode"] == "postarm_closed_1m"
    assert trace["confirmed_1m_close"] == good.close
    assert intent.stop_price == setup.stop_price
    assert intent.target_price == setup.target_price
    assert intent.metadata["micro_confirmation"]["candle_age_ms"] == 0


@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_breakout_ignores_unfinished_candle_and_stale_candle(monkeypatch, direction):
    now = int(time.time()*1000)
    monkeypatch.setattr("app.strategy.entry_engine_v2.time.time", lambda: now/1000)
    setup = _setup(now, direction)
    engine = EntryLifecycleV2()
    bullish = direction == Direction.LONG
    o, c = ((99.92, 100.07) if bullish else (100.08, 99.93))
    forming = Candle(now - 10_000, o, max(o,c)+.03, min(o,c)-.03, c, 100)
    status, intent, trace = engine.trigger(setup, _snap(now, direction, in_progress=forming), "forming")
    assert status == "pending" and trace["reason"] == "waiting_closed_1m_confirmation"
    stale = Candle(now - 180_000, o, max(o,c)+.03, min(o,c)-.03, c, 100)
    status, intent, trace = engine.trigger(setup, _snap(now, direction, stale), "stale")
    assert status == "pending" and trace["reason"] == "confirmation_1m_stale"
    assert engine.consumed_record(setup.setup_id) is None
