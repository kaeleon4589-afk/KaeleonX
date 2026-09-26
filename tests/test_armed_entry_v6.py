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


def test_v6_cancels_setup_instead_of_chasing_price_outside_entry_zone():
    engine = ArmedEntryEngine(ttl_seconds=600)
    setup = engine.discover(
        _range_regime(), _range_sweep_snapshot(), "TEST", "5m",
        {"active": "RANGE", "scores": {"VOLATILE_SWEEP": 2}},
    )
    assert setup is not None
    snap = _range_sweep_snapshot()
    snap.timeframes["1m"] = [Candle(i * 60_000, 100, 101, 99.9, 100.8, 100) for i in range(30)]
    snap.ask = setup.entry_zone_high + 0.01
    snap.bid = snap.ask - 0.01
    status, intent, trace = engine.trigger(setup, snap, "decision-3")
    assert status == "cancelled"
    assert intent is None
    assert trace["reason"] == "setup_chased"


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
