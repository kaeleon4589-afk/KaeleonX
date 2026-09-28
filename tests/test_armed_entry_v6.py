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


def _sweep_regime(direction=Direction.BULLISH):
    return SimpleNamespace(
        hard_block=False,
        sweep_allowed=True,
        breakout_allowed=False,
        risk_multiplier=0.80,
        direction=direction,
    )


def _sweep_meta(active="VOLATILE_SWEEP", trend_bias="long"):
    return {
        "active": active,
        "scores": {"VOLATILE_SWEEP": 4},
        "features": {"trend_bias": trend_bias},
    }


def test_v6_can_arm_high_quality_sweep_when_aligned_with_bullish_trend():
    engine = ArmedEntryEngine(ttl_seconds=600)
    setup = engine.discover(
        _sweep_regime(),
        _range_sweep_snapshot(),
        "TEST",
        "5m",
        _sweep_meta(),
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
        _sweep_regime(), base, "TEST", "5m",
        _sweep_meta(),
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
        _sweep_regime(), _range_sweep_snapshot(), "TEST", "5m",
        _sweep_meta(),
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
        _sweep_regime(), _range_sweep_snapshot(), "TEST", "5m",
        _sweep_meta(),
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
        _sweep_regime(), base, "TEST", "5m",
        _sweep_meta(),
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
        _sweep_regime(), base, "TEST", "5m",
        _sweep_meta(),
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
        _sweep_regime(), base, "TEST", "5m",
        _sweep_meta(),
    )
    assert rediscovered is None
    assert engine.last_trace["reason"] == "setup_consumed_waiting_new_structure"
    assert setup.setup_id in engine.last_trace["consumed_setup_ids"]


def test_v6_setup_expiry_is_bounded():
    engine = ArmedEntryEngine(ttl_seconds=60)
    setup = engine.discover(
        _sweep_regime(), _range_sweep_snapshot(), "TEST", "5m",
        _sweep_meta(),
    )
    assert setup is not None
    expired = setup.__class__(**{**setup.__dict__, "expires_at_ms": int(time.time() * 1000) - 1})
    status, intent, trace = engine.trigger(expired, _range_sweep_snapshot(), "decision-4")
    assert status == "cancelled"
    assert intent is None
    assert trace["reason"] == "setup_expired"


def test_v64_liquidity_sweep_blocks_countertrend_and_neutral_contexts():
    engine = ArmedEntryEngine(ttl_seconds=600)
    snap = _range_sweep_snapshot()

    # The fixture contains a valid LONG sweep. It is accepted in bullish trend.
    aligned = engine.discover(_sweep_regime(Direction.BULLISH), snap, "TEST", "5m", _sweep_meta())
    assert aligned is not None
    assert aligned.strategy == Strategy.LIQUIDITY_SWEEP
    assert aligned.direction == Direction.LONG
    assert aligned.metadata["trend_aligned"] is True
    assert aligned.metadata["trend_direction_at_arm"] == "LONG"

    # The same long sweep is forbidden when the detected trend is bearish.
    blocked = engine.discover(
        _sweep_regime(Direction.BEARISH), snap, "TEST-BEAR", "5m",
        _sweep_meta(trend_bias="short"),
    )
    assert blocked is None
    assert engine.last_trace["branches"]["sweep"]["reason"] == "no_liquidity_sweep_to_arm"

    # No directional trend => no liquidity-sweep setup at all.
    neutral = engine.discover(
        _sweep_regime(Direction.NEUTRAL), snap, "TEST-NEUTRAL", "5m",
        _sweep_meta(trend_bias="neutral"),
    )
    assert neutral is None
    assert engine.last_trace["reason"] == "liquidity_sweep_no_directional_trend"




def test_v65_trend_continuation_evaluates_aligned_sweep_without_volatile_score_gate():
    engine = ArmedEntryEngine(ttl_seconds=600)
    regime = SimpleNamespace(
        hard_block=False, sweep_allowed=False, breakout_allowed=True,
        risk_multiplier=1.0, direction=Direction.BULLISH,
    )
    meta = {
        "active": "TREND_CONTINUATION",
        "scores": {"TREND_CONTINUATION": 6, "VOLATILE_SWEEP": 0, "RANGE": 1},
        "features": {"trend_bias": "long"},
    }
    setup = engine.discover(regime, _range_sweep_snapshot(), "TEST-TREND", "5m", meta)
    assert setup is not None
    assert setup.strategy == Strategy.LIQUIDITY_SWEEP
    assert setup.direction == Direction.LONG
    assert setup.metadata["trend_aligned"] is True
    assert setup.metadata["trend_direction_at_arm"] == "LONG"
    assert engine.last_trace["branches"]["sweep"]["accepted"] is True


def test_v65_breakout_arm_window_allows_up_to_five_5m_retest_bars():
    from app.strategy import armed_entry
    assert armed_entry.BREAKOUT_ARM_MAX_RETEST_BARS == 5


def test_v64_range_is_shadow_only_for_liquidity_sweep():
    engine = ArmedEntryEngine(ttl_seconds=600)
    range_regime = SimpleNamespace(
        hard_block=False, sweep_allowed=False, breakout_allowed=False,
        risk_multiplier=0.65, direction=Direction.BULLISH,
    )
    setup = engine.discover(
        range_regime, _range_sweep_snapshot(), "TEST", "5m",
        _sweep_meta(active="RANGE", trend_bias="long"),
    )
    assert setup is None
    assert engine.last_trace["branches"]["sweep"]["reason"] == "regime_sweep_not_allowed"


def test_v64_old_persisted_liquidity_setup_cannot_trigger_without_alignment_stamp():
    engine = ArmedEntryEngine(ttl_seconds=600)
    setup = _fast_confirm_setup(Direction.SHORT)
    legacy = setup.__class__(**{**setup.__dict__, "metadata": {"atr_value": 1.0}})
    status, intent, trace = engine.trigger(legacy, SimpleNamespace(), "legacy")
    assert status == "cancelled"
    assert intent is None
    assert trace["reason"] == "liquidity_sweep_legacy_unaligned_setup"


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
                  "target_front_run_ratio": 1.0, "trend_aligned": True,
                  "trend_direction_at_arm": "SHORT", "trend_alignment_guard_version": 1},
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


def test_v64_breakout_has_priority_over_liquidity_sweep_in_trend_regime():
    from app.models.trading import ArmedSetup

    engine = ArmedEntryEngine(ttl_seconds=600)
    now = int(time.time() * 1000)
    breakout_setup = ArmedSetup(
        setup_id="PRIO:BR:1:LONG", symbol="PRIO", strategy=Strategy.BREAKOUT_RETEST,
        direction=Direction.LONG, armed_at_ms=now, expires_at_ms=now + 300_000,
        trigger_price=100.0, invalidation_price=99.0, stop_price=99.0,
        target_price=101.5, entry_zone_low=100.0, entry_zone_high=100.4,
        quality=72.0, risk_multiplier=1.0, timeframe="5m", metadata={"atr_value": 1.0},
    )
    sweep_setup = ArmedSetup(
        setup_id="PRIO:LS:1:LONG", symbol="PRIO", strategy=Strategy.LIQUIDITY_SWEEP,
        direction=Direction.LONG, armed_at_ms=now, expires_at_ms=now + 300_000,
        trigger_price=100.0, invalidation_price=99.0, stop_price=99.0,
        target_price=101.3, entry_zone_low=100.0, entry_zone_high=100.4,
        quality=99.0, risk_multiplier=1.0, timeframe="5m",
        metadata={"atr_value": 1.0, "trend_aligned": True,
                  "trend_direction_at_arm": "LONG", "trend_alignment_guard_version": 1},
    )
    sweep_calls = {"count": 0}
    engine._discover_breakout = lambda *args, **kwargs: breakout_setup

    def fake_sweep(*args, **kwargs):
        sweep_calls["count"] += 1
        return sweep_setup

    engine._discover_sweep = fake_sweep
    regime = SimpleNamespace(
        hard_block=False, breakout_allowed=True, sweep_allowed=False,
        risk_multiplier=1.0, direction=Direction.BULLISH,
    )
    selected = engine.discover(
        regime, SimpleNamespace(), "PRIO", "5m",
        {"active": "TREND_CONTINUATION", "scores": {"VOLATILE_SWEEP": 4},
         "features": {"trend_bias": "long"}},
    )
    assert selected is breakout_setup
    assert sweep_calls["count"] == 0
    assert engine.last_trace["branches"]["sweep"]["reason"] == "primary_breakout_selected"


def _breakout_precursor_snapshot():
    c5 = [
        Candle(i * 300_000, 100.0, 100.6, 99.4, 100.0, 100.0)
        for i in range(260)
    ]
    # Fresh breakout exists now, but there is no subsequent retest candle yet.
    c5[259] = Candle(259 * 300_000, 100.2, 101.2, 100.1, 100.95, 220.0)
    c15 = [Candle(i * 900_000, 100.0, 100.6, 99.4, 100.0, 100.0) for i in range(220)]
    c1h = [Candle(i * 3_600_000, 100.0, 100.6, 99.4, 100.0, 100.0) for i in range(220)]
    return SimpleNamespace(
        symbol="BR", timeframe="5m", candles=c5,
        timeframes={"5m": c5, "15m": c15, "1h": c1h},
        bid=100.90, ask=100.92, last=100.91,
    )


def _breakout_retest_snapshot():
    base = _breakout_precursor_snapshot()
    c5 = list(base.timeframes["5m"])
    c5.append(Candle(260 * 300_000, 100.78, 100.86, 100.44, 100.64, 150.0))
    return SimpleNamespace(
        symbol="BR", timeframe="5m", candles=c5,
        timeframes={"5m": c5, "15m": base.timeframes["15m"], "1h": base.timeframes["1h"]},
        bid=100.60, ask=100.62, last=100.61,
    )


def _sweep_precursor_snapshot():
    candles = [
        Candle(i * 300_000, 100.1, 101.0, 100.0, 100.2, 100.0)
        for i in range(260)
    ]
    candles[230] = Candle(230 * 300_000, 100.1, 102.5, 100.0, 100.2, 100.0)
    # Price is approaching the lower liquidity pool but has not swept it yet.
    candles[259] = Candle(259 * 300_000, 100.35, 100.55, 100.25, 100.30, 120.0)
    return SimpleNamespace(
        symbol="SW", timeframe="5m", candles=candles,
        timeframes={"5m": candles}, bid=100.29, ask=100.31, last=100.30,
    )


def _sweep_reclaim_snapshot():
    base = _sweep_precursor_snapshot()
    candles = list(base.candles)
    candles.append(Candle(260 * 300_000, 100.2, 100.35, 99.55, 100.18, 240.0))
    return SimpleNamespace(
        symbol="SW", timeframe="5m", candles=candles,
        timeframes={"5m": candles}, bid=100.17, ask=100.19, last=100.18,
    )


def test_v66_breakout_precursor_enters_watching_before_retest_and_then_arms():
    engine = ArmedEntryEngine(ttl_seconds=600, watch_ttl_seconds=1800)
    regime = SimpleNamespace(
        hard_block=False, sweep_allowed=False, breakout_allowed=True,
        risk_multiplier=1.0, direction=Direction.BULLISH,
    )
    meta = {"active": "TREND_CONTINUATION", "features": {"trend_bias": "long"}}

    # The complete-entry detector correctly says "not armed yet".
    immediate = engine.discover(regime, _breakout_precursor_snapshot(), "BR", "5m", meta)
    assert immediate is None

    # The new precursor path starts tracking the breakout instead of discarding it.
    watch = engine.discover_watch(regime, _breakout_precursor_snapshot(), "BR", "5m", meta)
    assert watch is not None
    assert watch.strategy == Strategy.BREAKOUT_RETEST
    assert watch.direction == Direction.LONG
    assert "waiting_retest" in watch.reasons
    assert watch.metadata["lifecycle_id"] == watch.watch_id
    assert engine.watch_trace["breakout"]["reason"] == "breakout_detected_waiting_retest"

    status, armed, trace = engine.advance_watch(watch, _breakout_retest_snapshot())
    assert status == "armed"
    assert armed is not None
    assert armed.strategy == Strategy.BREAKOUT_RETEST
    assert armed.direction == Direction.LONG
    assert armed.metadata["lifecycle_id"] == watch.watch_id
    assert armed.metadata["origin_watch_id"] == watch.watch_id
    assert trace["lifecycle_id"] == watch.watch_id
    assert trace["reason"] == "setup_armable"


def test_v66_breakout_uses_regime_trend_as_direction_authority_when_mtf_bias_is_none():
    engine = ArmedEntryEngine(ttl_seconds=600, watch_ttl_seconds=1800)
    regime = SimpleNamespace(
        hard_block=False, sweep_allowed=False, breakout_allowed=True,
        risk_multiplier=1.0, direction=Direction.BULLISH,
    )
    meta = {"active": "TREND_CONTINUATION", "features": {"trend_bias": "long"}}
    # Flat 1h/15m fixtures intentionally produce bias='none'. They no longer
    # erase a clear BULLISH+long regime; explicit opposite MTF bias still blocks.
    watch = engine.discover_watch(regime, _breakout_precursor_snapshot(), "BR", "5m", meta)
    assert watch is not None
    assert watch.metadata["bias_1h"] == "none"
    assert watch.metadata["bias_15m"] == "none"
    assert watch.direction == Direction.LONG


def test_v66_liquidity_precursor_watches_then_arms_only_with_trend_aligned_reclaim():
    engine = ArmedEntryEngine(ttl_seconds=600, watch_ttl_seconds=1800)
    regime = SimpleNamespace(
        hard_block=False, sweep_allowed=False, breakout_allowed=True,
        risk_multiplier=1.0, direction=Direction.BULLISH,
    )
    meta = {"active": "TREND_CONTINUATION", "features": {"trend_bias": "long"}}
    watch = engine.discover_watch(regime, _sweep_precursor_snapshot(), "SW", "5m", meta)
    assert watch is not None
    assert watch.strategy == Strategy.LIQUIDITY_SWEEP
    assert watch.direction == Direction.LONG
    assert watch.metadata["trend_direction"] == "LONG"
    assert watch.metadata["lifecycle_id"] == watch.watch_id

    status, armed, trace = engine.advance_watch(watch, _sweep_reclaim_snapshot())
    assert status == "armed"
    assert armed is not None
    assert armed.strategy == Strategy.LIQUIDITY_SWEEP
    assert armed.direction == Direction.LONG
    assert armed.metadata["trend_aligned"] is True
    assert armed.metadata["trend_direction_at_arm"] == "LONG"
    assert armed.metadata["lifecycle_id"] == watch.watch_id
    assert armed.metadata["origin_watch_id"] == watch.watch_id


def test_v66_watch_expiry_is_terminal_and_does_not_arm_stale_precursor():
    from app.models.trading import SetupWatch
    engine = ArmedEntryEngine(ttl_seconds=600, watch_ttl_seconds=1800)
    now = int(time.time() * 1000)
    watch = SetupWatch(
        watch_id="STALE:BRW:1:LONG", symbol="BR", strategy=Strategy.BREAKOUT_RETEST,
        direction=Direction.LONG, created_at_ms=now - 10_000,
        expires_at_ms=now - 1, timeframe="5m",
        metadata={"risk_multiplier": 1.0, "active_regime": "TREND_CONTINUATION"},
    )
    status, armed, trace = engine.advance_watch(watch, _breakout_retest_snapshot())
    assert status == "cancelled"
    assert armed is None
    assert trace["reason"] == "watch_expired"


def test_orchestrator_v66_precursor_moves_watching_to_armed_without_scanner_rotation():
    import asyncio
    from app.execution.paper import PaperExecutionEngine
    from app.models.trading import SetupWatch, ArmedSetup
    from test_core_pipeline_refactor import build_orchestrator, snapshot as base_snapshot

    class WatchRouter:
        def __init__(self):
            self.last_trace = {}
            self.watch = None

        def discover_armed(self, *args, **kwargs):
            self.last_trace = {"reason": "no_armable_setup", "armed": {"accepted": False, "reason": "no_fresh_breakout_retest_arm"}}
            return None

        def discover_watch(self, regime, snapshot, symbol, timeframe, regime_metadata=None):
            now = int(time.time() * 1000)
            self.watch = SetupWatch(
                watch_id="BTC:BRW:1:LONG", symbol=symbol, strategy=Strategy.BREAKOUT_RETEST,
                direction=Direction.LONG, created_at_ms=now, expires_at_ms=now + 600_000,
                timeframe=timeframe, reasons=("breakout_detected", "waiting_retest"),
                metadata={"risk_multiplier": 1.0, "active_regime": "TREND_CONTINUATION"},
            )
            self.last_trace = {"reason": "setup_watching", "watch": {"accepted": True, "reason": "breakout_detected_waiting_retest"}}
            return self.watch

        def advance_watch(self, watch, snapshot):
            now = int(time.time() * 1000)
            armed = ArmedSetup(
                setup_id="BTC:BR:1:LONG", symbol=watch.symbol, strategy=Strategy.BREAKOUT_RETEST,
                direction=Direction.LONG, armed_at_ms=now, expires_at_ms=now + 600_000,
                trigger_price=100.0, invalidation_price=99.0, stop_price=99.0,
                target_price=101.5, entry_zone_low=100.0, entry_zone_high=100.5,
                quality=82.0, risk_multiplier=1.0, timeframe="5m", metadata={"atr_value": 1.0},
            )
            self.last_trace = {"reason": "watch_armed", "watch": {"status": "armed", "reason": "retest_completed"}}
            return "armed", armed, {"reason": "retest_completed"}

        def trigger_armed(self, *args, **kwargs):
            raise AssertionError("watch monitor should only promote to ARMED in this test")

        def evaluate(self, *args, **kwargs):
            raise AssertionError("legacy path should not execute")

    execution = PaperExecutionEngine(initial_equity=100)
    orchestrator, manager, _, _, _ = build_orchestrator(execution, mode="demo")
    orchestrator.router = WatchRouter()
    snap = base_snapshot("BTC", 100.0)
    snap.timeframes = {"5m": snap.candles}

    first = asyncio.run(orchestrator.on_snapshot(snap, 100, user_id="u1"))
    assert first is None
    assert "BTC" in orchestrator.watching_setups
    assert "BTC" not in orchestrator.armed_setups

    watch_snap = base_snapshot("BTC", 100.0)
    watch_snap.timeframes = {"5m": watch_snap.candles}
    watch_snap.monitor_only = True
    watch_snap.watch_monitor = True
    watch_snap.armed_monitor = False
    promoted = asyncio.run(orchestrator.on_snapshot(watch_snap, 100, user_id="u1"))
    assert promoted is None
    assert "BTC" not in orchestrator.watching_setups
    assert "BTC" in orchestrator.armed_setups
    promoted_setup = orchestrator.armed_setups["BTC"]
    assert promoted_setup.metadata["lifecycle_id"] == "BTC:BRW:1:LONG"
    assert promoted_setup.metadata["origin_watch_id"] == "BTC:BRW:1:LONG"
    assert not manager.positions


def _v68_near_like_postarm_snapshot(setup, *, orderbook_valid=True):
    """Reproduce the 2026-09-28 NEAR shape: strong bearish close, ~0.52 RVOL."""
    close_ms = setup.armed_at_ms + 60_000
    start = close_ms - 20 * 60_000
    candles = [
        Candle(start + i * 60_000, 100.00, 100.05, 99.95, 100.00, 100.0)
        for i in range(20)
    ]
    # Bearish body=0.50, close position=0.20, RVOL=0.52: strong directional
    # rejection but below the legacy 0.70 RVOL floor.
    candles[-1] = Candle(
        close_ms - 60_000,
        100.04,
        100.10,
        99.90,
        99.94,
        52.0,
    )
    return SimpleNamespace(
        symbol="FAST",
        ask=99.99,
        bid=99.98,
        last=99.985,
        timeframes={"1m": candles},
        quote_received_ms=close_ms + 2_000,
        orderbook_valid=orderbook_valid,
        bids=[(99.98, 5.0), (99.97, 4.0), (99.96, 3.0)] if orderbook_valid else [],
        asks=[(99.99, 5.0), (100.00, 4.0), (100.01, 3.0)] if orderbook_valid else [],
    )


def test_v68_strong_postarm_shape_with_moderate_volume_can_confirm_with_valid_book():
    setup = _fast_confirm_setup(Direction.SHORT)
    setup = setup.__class__(**{
        **setup.__dict__,
        "quality": 73.21,
    })
    engine = ArmedEntryEngine(
        ttl_seconds=600,
        chase_tolerance_atr=0.15,
        trigger_close_tolerance_atr=0.08,
        fast_confirm_enabled=True,
        fast_confirm_max_age_seconds=30,
    )
    snap = _v68_near_like_postarm_snapshot(setup, orderbook_valid=True)

    status, intent, trace = engine.trigger(setup, snap, "decision-v68-adaptive")

    assert status == "triggered"
    assert intent is not None
    assert trace["confirmation_mode"] == "postarm_closed_1m_strong_shape"
    assert trace["postarm_adaptive_used"] is True
    assert trace["1m_rvol"] == 0.52
    assert trace["1m_adaptive_shape_ok"] is True
    assert trace["execution_rr"] >= 1.10
    assert "micro_confirmation_1m_adaptive" in intent.reasons


def test_v68_adaptive_postarm_confirmation_keeps_quality_and_orderbook_guards():
    for quality, book_valid in ((71.99, True), (73.21, False)):
        setup = _fast_confirm_setup(Direction.SHORT)
        setup = setup.__class__(**{
            **setup.__dict__,
            "quality": quality,
        })
        engine = ArmedEntryEngine(
            ttl_seconds=600,
            chase_tolerance_atr=0.15,
            trigger_close_tolerance_atr=0.08,
        )
        snap = _v68_near_like_postarm_snapshot(setup, orderbook_valid=book_valid)

        status, intent, trace = engine.trigger(
            setup, snap, f"decision-v68-guard-{quality}-{book_valid}"
        )

        assert status == "pending"
        assert intent is None
        assert trace["reason"] == "waiting_1m_confirmation"
        assert trace["postarm_adaptive_candidate"] is True
        assert trace["postarm_adaptive_book_valid"] is book_valid
        assert trace["postarm_adaptive_setup_quality_ok"] is (quality >= 72.0)
