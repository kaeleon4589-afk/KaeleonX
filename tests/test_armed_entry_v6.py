from __future__ import annotations

import time
from types import SimpleNamespace

from app.models.enums import Direction, Strategy
from app.models.market import Candle
from app.strategy.armed_entry import ArmedEntryEngine


def _range_sweep_snapshot():
    candles = [
        Candle(i * 300_000, 100.1, 101.0, 100.0, 100.2, 100.0)
        for i in range(260)
    ]
    # Historical opposing liquidity for a meaningful target.
    candles[230] = Candle(230 * 300_000, 100.1, 102.5, 100.0, 100.2, 100.0)
    # Sweep below the 34-bar liquidity floor, reclaim, then stable follow-through.
    candles[258] = Candle(258 * 300_000, 100.2, 100.3, 99.6, 100.15, 240.0)
    candles[259] = Candle(259 * 300_000, 100.12, 100.4, 99.9, 100.28, 150.0)
    return SimpleNamespace(
        symbol="TEST",
        timeframe="5m",
        candles=candles,
        timeframes={"5m": candles},
        bid=100.18,
        ask=100.20,
        last=100.19,
    )


def _range_regime():
    return SimpleNamespace(
        hard_block=False,
        sweep_allowed=False,
        breakout_allowed=False,
        risk_multiplier=0.65,
    )


def test_v6_can_arm_high_quality_sweep_while_regime_is_range():
    engine = ArmedEntryEngine(ttl_seconds=600)
    setup = engine.discover(
        _range_regime(),
        _range_sweep_snapshot(),
        "TEST",
        "5m",
        {"active": "RANGE", "scores": {"VOLATILE_SWEEP": 2}},
    )
    assert setup is not None
    assert setup.strategy == Strategy.LIQUIDITY_SWEEP
    assert setup.direction == Direction.LONG
    assert setup.entry_zone_low <= setup.trigger_price <= setup.entry_zone_high
    assert setup.stop_price < setup.trigger_price < setup.target_price
    assert setup.metadata["strategy_model"] == "armed_liquidity_sweep_v6"
    assert setup.metadata["stop_atr_5m"] < 0.90  # old global floor would have rejected it


def test_v6_waits_for_closed_1m_confirmation_then_builds_trade_intent():
    engine = ArmedEntryEngine(ttl_seconds=600)
    base = _range_sweep_snapshot()
    setup = engine.discover(
        _range_regime(), base, "TEST", "5m",
        {"active": "RANGE", "scores": {"VOLATILE_SWEEP": 2}},
    )
    assert setup is not None

    # Live quote has crossed the trigger, but without 1m confirmation the setup stays armed.
    waiting = SimpleNamespace(**vars(base))
    waiting.ask = setup.trigger_price + 0.02
    waiting.bid = waiting.ask - 0.02
    status, intent, trace = engine.trigger(setup, waiting, "decision-1")
    assert status == "pending"
    assert intent is None
    assert trace["reason"] == "waiting_1m_confirmation"

    stale = SimpleNamespace(**vars(base))
    stale.timeframes = {
        "5m": base.candles,
        "1m": [Candle(setup.armed_at_ms - 120_000, 100.0, 100.3, 99.9, setup.trigger_price + 0.05, 180.0)],
    }
    stale.ask = setup.trigger_price + 0.03
    stale.bid = stale.ask - 0.02
    status, intent, trace = engine.trigger(setup, stale, "decision-stale")
    assert status == "pending"
    assert intent is None
    assert trace["reason"] == "waiting_fresh_1m_confirmation"

    micro_start = setup.armed_at_ms - 29 * 60_000
    micro = [
        Candle(micro_start + i * 60_000, 100.08, 100.16, 100.02, 100.10, 100.0)
        for i in range(30)
    ]
    close = setup.trigger_price + 0.05
    micro[-1] = Candle(setup.armed_at_ms + 1, setup.trigger_price - 0.04, close + 0.02,
                       setup.trigger_price - 0.05, close, 180.0)
    triggered = SimpleNamespace(**vars(base))
    triggered.timeframes = {"5m": base.candles, "1m": micro}
    triggered.ask = setup.trigger_price + 0.03
    triggered.bid = triggered.ask - 0.02

    status, intent, trace = engine.trigger(setup, triggered, "decision-2")
    assert status == "triggered"
    assert intent is not None
    assert intent.metadata["entry_model"] == "armed_v6"
    assert intent.metadata["armed_setup_id"] == setup.setup_id
    assert intent.stop_price < intent.entry_price < intent.target_price
    assert trace["execution_rr"] >= 1.10


def test_v61_small_overshoot_does_not_cancel_setup_as_chased():
    engine = ArmedEntryEngine(ttl_seconds=600, chase_tolerance_atr=0.15)
    setup = engine.discover(
        _range_regime(), _range_sweep_snapshot(), "TEST", "5m",
        {"active": "RANGE", "scores": {"VOLATILE_SWEEP": 2}},
    )
    assert setup is not None
    atr_value = float(setup.metadata["atr_value"])
    snap = _range_sweep_snapshot()
    # This is outside the old hard boundary but well inside the new 0.15 ATR buffer.
    snap.ask = setup.entry_zone_high + atr_value * 0.05
    snap.bid = snap.ask - atr_value * 0.01
    status, intent, trace = engine.trigger(setup, snap, "decision-small-overshoot")
    assert status == "pending"
    assert intent is None
    assert trace["reason"] in {"waiting_1m_confirmation", "waiting_fresh_1m_confirmation"}


def test_v61_cancels_setup_only_after_price_exceeds_atr_chase_buffer():
    engine = ArmedEntryEngine(ttl_seconds=600, chase_tolerance_atr=0.15)
    setup = engine.discover(
        _range_regime(), _range_sweep_snapshot(), "TEST", "5m",
        {"active": "RANGE", "scores": {"VOLATILE_SWEEP": 2}},
    )
    assert setup is not None
    atr_value = float(setup.metadata["atr_value"])
    snap = _range_sweep_snapshot()
    snap.ask = setup.entry_zone_high + atr_value * 0.22
    snap.bid = snap.ask - atr_value * 0.01
    status, intent, trace = engine.trigger(setup, snap, "decision-3")
    assert status == "cancelled"
    assert intent is None
    assert trace["reason"] == "setup_chased"
    assert trace["chase_limit"] > setup.entry_zone_high


def test_v61_fresh_1m_close_can_confirm_inside_trigger_tolerance_band():
    engine = ArmedEntryEngine(
        ttl_seconds=600, chase_tolerance_atr=0.15, trigger_close_tolerance_atr=0.08
    )
    base = _range_sweep_snapshot()
    setup = engine.discover(
        _range_regime(), base, "TEST", "5m",
        {"active": "RANGE", "scores": {"VOLATILE_SWEEP": 2}},
    )
    assert setup is not None
    atr_value = float(setup.metadata["atr_value"])
    close = setup.trigger_price - atr_value * 0.04
    micro_start = setup.armed_at_ms - 29 * 60_000
    micro = [
        Candle(micro_start + i * 60_000, close - 0.02, close + 0.03, close - 0.03, close, 100.0)
        for i in range(30)
    ]
    micro[-1] = Candle(
        setup.armed_at_ms + 1,
        close - atr_value * 0.04,
        close + atr_value * 0.02,
        close - atr_value * 0.05,
        close,
        180.0,
    )
    snap = SimpleNamespace(**vars(base))
    snap.timeframes = {"5m": base.candles, "1m": micro}
    snap.ask = setup.trigger_price + atr_value * 0.01
    snap.bid = snap.ask - atr_value * 0.01

    status, intent, trace = engine.trigger(setup, snap, "decision-trigger-band")
    assert status == "triggered"
    assert intent is not None
    assert trace["reason"] == "micro_confirmation_1m"
    assert close < setup.trigger_price
    assert setup.trigger_price - close <= trace["trigger_close_buffer"]


def test_v61_cancelled_structure_is_consumed_and_not_rearmed():
    engine = ArmedEntryEngine(
        ttl_seconds=600, chase_tolerance_atr=0.15, consumed_ttl_seconds=3600
    )
    base = _range_sweep_snapshot()
    setup = engine.discover(
        _range_regime(), base, "TEST", "5m",
        {"active": "RANGE", "scores": {"VOLATILE_SWEEP": 2}},
    )
    assert setup is not None
    atr_value = float(setup.metadata["atr_value"])
    chased = SimpleNamespace(**vars(base))
    chased.ask = setup.entry_zone_high + atr_value * 0.22
    chased.bid = chased.ask - atr_value * 0.01
    status, intent, trace = engine.trigger(setup, chased, "decision-consume")
    assert status == "cancelled"
    assert intent is None
    assert trace["reason"] == "setup_chased"

    rediscovered = engine.discover(
        _range_regime(), base, "TEST", "5m",
        {"active": "RANGE", "scores": {"VOLATILE_SWEEP": 2}},
    )
    assert rediscovered is None
    assert engine.last_trace["reason"] == "setup_consumed_waiting_new_structure"
    assert setup.setup_id in engine.last_trace["consumed_setup_ids"]


def test_v6_setup_expiry_is_bounded():
    engine = ArmedEntryEngine(ttl_seconds=60)
    setup = engine.discover(
        _range_regime(), _range_sweep_snapshot(), "TEST", "5m",
        {"active": "RANGE", "scores": {"VOLATILE_SWEEP": 2}},
    )
    assert setup is not None
    expired = setup.__class__(**{**setup.__dict__, "expires_at_ms": int(time.time() * 1000) - 1})
    status, intent, trace = engine.trigger(expired, _range_sweep_snapshot(), "decision-4")
    assert status == "cancelled"
    assert intent is None
    assert trace["reason"] == "setup_expired"


def test_orchestrator_v6_arms_first_and_executes_only_after_trigger():
    import asyncio

    from app.execution.paper import PaperExecutionEngine
    from app.models.trading import ArmedSetup, TradeIntent
    from test_core_pipeline_refactor import build_orchestrator, snapshot as base_snapshot

    class ArmedRouter:
        def __init__(self):
            self.last_trace = {}
            self.discover_calls = 0
            self.trigger_calls = 0

        def discover_armed(self, regime, snapshot, symbol, timeframe, regime_metadata=None):
            self.discover_calls += 1
            self.last_trace = {"reason": "setup_armed", "armed": {"accepted": True, "reason": "setup_armed"}}
            return ArmedSetup(
                setup_id="arm-1", symbol=symbol, strategy=Strategy.BREAKOUT_RETEST,
                direction=Direction.LONG, armed_at_ms=int(time.time() * 1000),
                expires_at_ms=int(time.time() * 1000) + 60_000,
                trigger_price=100.0, invalidation_price=99.0,
                stop_price=99.0, target_price=102.0,
                entry_zone_low=100.0, entry_zone_high=100.5,
                quality=88.0, risk_multiplier=1.0, timeframe=timeframe,
                metadata={"atr_value": 1.0},
            )

        def trigger_armed(self, setup, snapshot, decision_id):
            self.trigger_calls += 1
            intent = TradeIntent(
                decision_id, setup.symbol, setup.strategy, setup.direction,
                100.0, setup.stop_price, setup.target_price, 90.0, 1.0, "5m",
                metadata={"atr_value": 1.0, "atr_pct": 0.01, "sl_pct": 0.01,
                          "entry_model": "armed_v6", "structural_rr_estimate": 2.0},
            )
            self.last_trace = {"reason": "armed_triggered", "armed": {"status": "triggered"}}
            return "triggered", intent, {"reason": "micro_confirmation_1m"}

        def evaluate(self, *args, **kwargs):
            raise AssertionError("legacy immediate-entry path must not run in v6")

    execution = PaperExecutionEngine(initial_equity=100)
    orchestrator, manager, _, _, _ = build_orchestrator(execution, mode="demo")
    router = ArmedRouter()
    orchestrator.router = router
    snap = base_snapshot("BTC", 100.0)
    snap.timeframes = {"1m": [Candle(0, 99.9, 100.1, 99.8, 100.0, 100.0)]}

    first = asyncio.run(orchestrator.on_snapshot(snap, 100, user_id="u1"))
    assert first is None
    assert not manager.positions
    assert "BTC" in orchestrator.armed_setups
    assert router.discover_calls == 1

    second = asyncio.run(orchestrator.on_snapshot(snap, 100, user_id="u1"))
    assert second is not None and second["filled"] is True
    assert manager.positions
    assert "BTC" not in orchestrator.armed_setups
    assert router.trigger_calls == 1


def _fast_confirm_setup(direction=Direction.LONG):
    from app.models.trading import ArmedSetup
    now = int(time.time() * 1000)
    if direction == Direction.LONG:
        return ArmedSetup(
            setup_id="FAST:BR:1:LONG", symbol="FAST", strategy=Strategy.BREAKOUT_RETEST,
            direction=direction, armed_at_ms=now, expires_at_ms=now + 300_000,
            trigger_price=100.0, invalidation_price=99.0, stop_price=99.0,
            target_price=102.0, entry_zone_low=100.0, entry_zone_high=100.5,
            quality=85.0, risk_multiplier=1.0, timeframe="5m",
            metadata={"atr_value": 1.0, "structural_target_price": 102.0,
                      "target_front_run_ratio": 1.0},
        )
    return ArmedSetup(
        setup_id="FAST:LS:1:SHORT", symbol="FAST", strategy=Strategy.LIQUIDITY_SWEEP,
        direction=direction, armed_at_ms=now, expires_at_ms=now + 300_000,
        trigger_price=100.0, invalidation_price=101.0, stop_price=101.0,
        target_price=98.7, entry_zone_low=99.5, entry_zone_high=100.0,
        quality=85.0, risk_multiplier=1.0, timeframe="5m",
        metadata={"atr_value": 1.0, "structural_target_price": 98.7,
                  "target_front_run_ratio": 1.0},
    )


def _recent_prearm_micro(setup, *, age_ms=10_000, strong=True):
    close_ms = setup.armed_at_ms - age_ms
    start = close_ms - 60_000 - 29 * 60_000
    candles = [
        Candle(start + i * 60_000, 99.90, 100.00, 99.80, 99.92, 100.0)
        for i in range(30)
    ]
    if setup.direction == Direction.LONG:
        if strong:
            candles[-1] = Candle(close_ms - 60_000, 99.74, 100.02, 99.70, 99.98, 180.0)
        else:
            candles[-1] = Candle(close_ms - 60_000, 99.95, 100.01, 99.90, 99.97, 80.0)
    else:
        if strong:
            candles[-1] = Candle(close_ms - 60_000, 100.26, 100.30, 99.98, 100.02, 180.0)
        else:
            candles[-1] = Candle(close_ms - 60_000, 100.05, 100.10, 99.99, 100.03, 80.0)
    return candles


def test_v63_recent_strong_prearm_closed_1m_can_fast_confirm():
    engine = ArmedEntryEngine(
        ttl_seconds=600, chase_tolerance_atr=0.15,
        trigger_close_tolerance_atr=0.08,
        fast_confirm_enabled=True, fast_confirm_max_age_seconds=30,
    )
    setup = _fast_confirm_setup(Direction.LONG)
    micro = _recent_prearm_micro(setup, age_ms=8_000, strong=True)
    snap = SimpleNamespace(
        symbol="FAST", ask=100.02, bid=100.01, last=100.015,
        timeframes={"1m": micro}, quote_received_ms=setup.armed_at_ms + 2_000,
        orderbook_valid=True,
        bids=[(100.01, 5.0), (100.00, 4.0), (99.99, 3.0)],
        asks=[(100.02, 5.0), (100.03, 4.0), (100.04, 3.0)],
    )
    status, intent, trace = engine.trigger(setup, snap, "decision-fast")
    assert status == "triggered"
    assert intent is not None
    assert trace["reason"] == "micro_confirmation_fast_1m"
    assert trace["confirmation_mode"] == "recent_prearm_closed_1m"
    assert trace["prearm_age_ms"] == 8_000
    assert trace["execution_rr"] >= 1.10
    assert "micro_confirmation_fast_1m" in intent.reasons


def test_v63_fast_confirmation_rejects_stale_or_weak_prearm_evidence():
    for age_ms, strong in ((45_000, True), (8_000, False)):
        engine = ArmedEntryEngine(
            ttl_seconds=600, chase_tolerance_atr=0.15,
            trigger_close_tolerance_atr=0.08,
            fast_confirm_enabled=True, fast_confirm_max_age_seconds=30,
        )
        setup = _fast_confirm_setup(Direction.LONG)
        micro = _recent_prearm_micro(setup, age_ms=age_ms, strong=strong)
        snap = SimpleNamespace(
            symbol="FAST", ask=100.02, bid=100.01, last=100.015,
            timeframes={"1m": micro}, quote_received_ms=setup.armed_at_ms + 2_000,
            orderbook_valid=True,
            bids=[(100.01, 5.0), (100.00, 4.0)], asks=[(100.02, 5.0), (100.03, 4.0)],
        )
        status, intent, trace = engine.trigger(setup, snap, f"decision-wait-{age_ms}-{strong}")
        assert status == "pending"
        assert intent is None
        assert trace["reason"] == "waiting_fresh_1m_confirmation"
        assert trace["confirmation_mode"] == "waiting_new_closed_1m"


def test_v63_fast_confirmation_requires_valid_orderbook():
    engine = ArmedEntryEngine(fast_confirm_enabled=True, fast_confirm_max_age_seconds=30)
    setup = _fast_confirm_setup(Direction.LONG)
    snap = SimpleNamespace(
        symbol="FAST", ask=100.02, bid=100.01, last=100.015,
        timeframes={"1m": _recent_prearm_micro(setup, age_ms=5_000, strong=True)},
        quote_received_ms=setup.armed_at_ms + 2_000,
        orderbook_valid=False, bids=[], asks=[],
    )
    status, intent, trace = engine.trigger(setup, snap, "decision-no-book")
    assert status == "pending"
    assert intent is None
    assert trace["reason"] == "waiting_fresh_1m_confirmation"
    assert trace["fast_confirm_book_valid"] is False


def test_v63_one_tick_chase_edge_is_tolerated_but_real_chase_still_cancels():
    engine = ArmedEntryEngine(chase_tolerance_atr=0.15)
    setup = _fast_confirm_setup(Direction.LONG)
    tiny = SimpleNamespace(
        symbol="FAST", ask=100.6505, bid=100.6495, last=100.65,
        timeframes={}, quote_received_ms=setup.armed_at_ms + 2_000,
        orderbook_valid=True,
        bids=[(100.6495, 5.0), (100.6485, 4.0)],
        asks=[(100.6505, 5.0), (100.6515, 4.0)],
    )
    status, intent, trace = engine.trigger(setup, tiny, "decision-edge")
    assert status == "pending"
    assert intent is None
    assert trace["reason"] == "waiting_1m_confirmation"

    engine2 = ArmedEntryEngine(chase_tolerance_atr=0.15)
    setup2 = _fast_confirm_setup(Direction.LONG)
    chased = SimpleNamespace(
        symbol="FAST", ask=100.68, bid=100.679, last=100.6795,
        timeframes={}, quote_received_ms=setup2.armed_at_ms + 2_000,
        orderbook_valid=True,
        bids=[(100.679, 5.0), (100.678, 4.0)],
        asks=[(100.68, 5.0), (100.681, 4.0)],
    )
    status, intent, trace = engine2.trigger(setup2, chased, "decision-real-chase")
    assert status == "cancelled"
    assert intent is None
    assert trace["reason"] == "setup_chased"
    assert trace["price"] > trace["chase_limit"]


def test_v63_recent_strong_prearm_closed_1m_fast_confirm_is_directionally_symmetric():
    engine = ArmedEntryEngine(
        ttl_seconds=600, chase_tolerance_atr=0.15,
        trigger_close_tolerance_atr=0.08,
        fast_confirm_enabled=True, fast_confirm_max_age_seconds=30,
    )
    setup = _fast_confirm_setup(Direction.SHORT)
    micro = _recent_prearm_micro(setup, age_ms=7_000, strong=True)
    snap = SimpleNamespace(
        symbol="FAST", ask=99.99, bid=99.98, last=99.985,
        timeframes={"1m": micro}, quote_received_ms=setup.armed_at_ms + 2_000,
        orderbook_valid=True,
        bids=[(99.98, 5.0), (99.97, 4.0), (99.96, 3.0)],
        asks=[(99.99, 5.0), (100.00, 4.0), (100.01, 3.0)],
    )
    status, intent, trace = engine.trigger(setup, snap, "decision-fast-short")
    assert status == "triggered"
    assert intent is not None
    assert trace["reason"] == "micro_confirmation_fast_1m"
    assert intent.direction == Direction.SHORT
    assert intent.target_price < intent.entry_price < intent.stop_price
    assert trace["execution_rr"] >= 1.10
