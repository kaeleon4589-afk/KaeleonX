from __future__ import annotations

from types import SimpleNamespace
from dataclasses import replace
import time

from app.models.enums import Direction, Strategy
from app.models.market import Candle
from app.strategy.breakout_retest_v2 import BreakoutRetestStrategyV2
from app.strategy.entry_engine_v2 import EntryLifecycleV2
from app.strategy.liquidity_sweep_v2 import LiquiditySweepStrategyV2


def _trend_frame(count=120, start=100.0, step=0.03, span=300_000):
    rows = []
    for i in range(count):
        base = start + i * step
        rows.append(Candle(i * span, base, base + 0.08, base - 0.06, base + 0.04, 100.0))
    return rows


def breakout_snapshot():
    c5 = _trend_frame()
    # Replace the tail with a clean breakout -> retest -> continuation sequence.
    prior_high = max(c.high for c in c5[92:116])
    c5[116] = Candle(116 * 300_000, prior_high - 0.03, prior_high + 0.24, prior_high - 0.07, prior_high + 0.17, 150)
    c5[117] = Candle(117 * 300_000, prior_high + 0.12, prior_high + 0.17, prior_high - 0.02, prior_high + 0.06, 120)
    c5[118] = Candle(118 * 300_000, prior_high + 0.05, prior_high + 0.21, prior_high + 0.03, prior_high + 0.17, 135)
    c5[119] = Candle(119 * 300_000, prior_high + 0.12, prior_high + 0.17, prior_high + 0.08, prior_high + 0.13, 115)
    c15 = _trend_frame(start=98.0, step=0.05, span=900_000)
    c1h = _trend_frame(start=90.0, step=0.09, span=3_600_000)
    return SimpleNamespace(
        symbol="TEST", timeframe="5m", candles=c5,
        timeframes={"5m": c5, "15m": c15, "1h": c1h},
        bid=c5[-1].close - 0.005, ask=c5[-1].close + 0.005,
        bids=[(c5[-1].close - 0.005, 50)] * 12,
        asks=[(c5[-1].close + 0.005, 45)] * 12,
        orderbook_valid=True, last=c5[-1].close,
    )


def range_snapshot():
    rows = []
    for i in range(120):
        center = 100.0 + (0.12 if i % 4 < 2 else -0.12)
        rows.append(Candle(i * 300_000, center, 100.85, 99.15, center + (0.03 if i % 2 == 0 else -0.03), 100.0))
    rows[117] = Candle(117 * 300_000, 99.35, 99.55, 98.82, 99.28, 145.0)
    rows[118] = Candle(118 * 300_000, 99.27, 99.60, 99.18, 99.48, 130.0)
    rows[119] = Candle(119 * 300_000, 99.42, 99.62, 99.34, 99.50, 120.0)
    return SimpleNamespace(
        symbol="RANGE", timeframe="5m", candles=rows,
        timeframes={"5m": rows}, bid=99.49, ask=99.51,
        bids=[(99.49, 60)] * 12, asks=[(99.51, 45)] * 12,
        orderbook_valid=True, last=99.50,
    )


def test_breakout_retest_v2_builds_ready_setup():
    regime = SimpleNamespace(hard_block=False, breakout_allowed=True, sweep_allowed=False,
                             direction=Direction.LONG, risk_multiplier=1.0)
    strategy = BreakoutRetestStrategyV2()
    candidate = strategy.scan(regime, breakout_snapshot())
    assert candidate is not None, strategy.last_trace
    assert candidate.stage == "READY"
    assert candidate.strategy == Strategy.BREAKOUT_RETEST
    assert candidate.direction == Direction.LONG
    assert candidate.stop_price < candidate.signal_entry < candidate.target_price
    assert candidate.metadata["execution_rr"] >= candidate.metadata["minimum_viable_rr"]
    assert candidate.metadata["target_model"] == "dynamic_structure_expansion"
    assert "target_rr_cap" not in candidate.metadata


def test_liquidity_sweep_v2_is_counter_edge_mean_reversion():
    regime = SimpleNamespace(hard_block=False, breakout_allowed=False, sweep_allowed=True,
                             direction=Direction.NEUTRAL, risk_multiplier=.8)
    strategy = LiquiditySweepStrategyV2()
    candidate = strategy.scan(regime, range_snapshot())
    assert candidate is not None, strategy.last_trace
    assert candidate.stage == "READY"
    assert candidate.strategy == Strategy.LIQUIDITY_SWEEP
    assert candidate.direction == Direction.LONG
    assert candidate.stop_price < candidate.signal_entry < candidate.target_price
    assert candidate.metadata["execution_rr"] >= candidate.metadata["minimum_viable_rr"]
    assert candidate.metadata["target_model"] == "dynamic_range_mean_reversion"
    assert candidate.target_price < candidate.metadata["opposite_range_level"]
    assert 0.56 <= candidate.metadata["mean_reversion_fraction"] <= 0.76
    assert "target_rr_cap" not in candidate.metadata


def test_v2_armed_entry_requires_micro_confirmation_and_is_single_use(monkeypatch):
    regime = SimpleNamespace(hard_block=False, breakout_allowed=True, sweep_allowed=False,
                             direction=Direction.LONG, risk_multiplier=1.0)
    snap = breakout_snapshot()
    candidate = BreakoutRetestStrategyV2().scan(regime, snap)
    assert candidate and candidate.stage == "READY"
    engine = EntryLifecycleV2(fast_confirm_enabled=False)
    setup = engine.arm(candidate, "TEST", "5m")
    assert setup is not None
    # This test exercises confirmation/single-use, not the strategy target
    # geometry; provide an achievable reward at the reclaimed trigger.
    target = setup.trigger_price + 2.0 * (setup.trigger_price - setup.stop_price)
    setup = replace(setup, target_price=target,
                    metadata={**setup.metadata, "structural_target_price": target})

    now = setup.armed_at_ms + 120_000
    monkeypatch.setattr("app.strategy.entry_engine_v2.time.time", lambda: now / 1000.0)
    trigger = setup.trigger_price
    micro = [Candle(now - (30-i)*60_000, trigger, trigger+.02, trigger-.02, trigger, 100) for i in range(30)]
    micro[-1] = Candle(now - 60_000, trigger - .03, trigger + .08, trigger - .04, trigger + .05, 160)
    quote = (setup.entry_zone_low + setup.entry_zone_high) / 2
    snap.timeframes["1m"] = micro
    snap.bid = quote - .005
    snap.ask = quote + .005
    snap.bids = [(snap.bid, 70)] * 12
    snap.asks = [(snap.ask, 45)] * 12
    status, intent, trace = engine.trigger(setup, snap, "decision-v2")
    assert status == "pending", trace
    assert intent is None
    assert trace["reason"] == "breakout_live_reclaim_pending"

    # A fresh quote reclaims the trigger geometry before submitting an order.
    snap.ask = setup.trigger_price - 0.09 * setup.metadata["atr_value"]
    snap.bid = snap.ask - .01
    snap.bids = [(snap.bid, 70)] * 12
    snap.asks = [(snap.ask, 45)] * 12
    status, intent, trace = engine.trigger(setup, snap, "decision-v2-reclaim")
    assert status == "triggered", trace
    assert intent is not None
    assert intent.metadata["engine_version"] == "v2"
    assert intent.metadata["execution_rr"] >= intent.metadata["minimum_viable_rr"]
    assert intent.target_price == setup.target_price
    assert intent.metadata["target_locked_at_arm"] is True

    status, duplicate, trace = engine.trigger(setup, snap, "decision-duplicate")
    assert status == "cancelled"
    assert duplicate is None
    assert trace["reason"] == "setup_already_consumed"


def _flat_frame(count=120, span=900_000, amplitude=.35):
    rows = []
    for i in range(count):
        center = 100.0 + (0.05 if i % 4 < 2 else -0.05)
        rows.append(Candle(i * span, center, 100.0 + amplitude, 100.0 - amplitude,
                           center + (0.01 if i % 2 == 0 else -0.01), 100.0))
    return rows


def test_regime_v2_does_not_call_flat_ema_stack_a_trend():
    from app.regime.regime_engine import RegimeEngine
    snap = range_snapshot()
    snap.timeframes["15m"] = _flat_frame(span=900_000)
    snap.timeframes["1h"] = _flat_frame(span=3_600_000, amplitude=.30)
    engine = RegimeEngine()
    regime = engine.evaluate_snapshot(snap)
    assert engine.last_metadata["active"] == "RANGE", engine.last_metadata
    assert regime.sweep_allowed is True
    assert regime.breakout_allowed is False


def test_regime_v2_routes_clean_multitimeframe_trend_to_breakout():
    from app.regime.regime_engine import RegimeEngine
    snap = breakout_snapshot()
    engine = RegimeEngine()
    regime = engine.evaluate_snapshot(snap)
    assert engine.last_metadata["active"] == "TREND", engine.last_metadata
    assert engine.last_metadata.get("trend_score") is not None
    assert engine.last_metadata.get("range_score") is not None
    assert engine.last_metadata.get("adx5") is not None
    assert regime.breakout_allowed is True
    assert regime.sweep_allowed is False
    assert regime.direction == Direction.LONG


def test_v2_targets_ignore_legacy_fixed_rr_environment(monkeypatch):
    breakout_regime = SimpleNamespace(hard_block=False, breakout_allowed=True, sweep_allowed=False,
                                      direction=Direction.LONG, risk_multiplier=1.0)
    sweep_regime = SimpleNamespace(hard_block=False, breakout_allowed=False, sweep_allowed=True,
                                   direction=Direction.NEUTRAL, risk_multiplier=.8)

    breakout_before = BreakoutRetestStrategyV2().scan(breakout_regime, breakout_snapshot())
    sweep_before = LiquiditySweepStrategyV2().scan(sweep_regime, range_snapshot())
    assert breakout_before is not None
    assert sweep_before is not None

    # These legacy variables may still exist in Railway during rollout. Engine V2
    # must ignore them completely.
    monkeypatch.setenv("TRADE_BREAKOUT_RETEST_TARGET_RR", "9.99")
    monkeypatch.setenv("TRADE_LIQUIDITY_SWEEP_TARGET_RR", "9.99")

    breakout_after = BreakoutRetestStrategyV2().scan(breakout_regime, breakout_snapshot())
    sweep_after = LiquiditySweepStrategyV2().scan(sweep_regime, range_snapshot())
    assert breakout_after is not None
    assert sweep_after is not None
    assert breakout_after.target_price == breakout_before.target_price
    assert sweep_after.target_price == sweep_before.target_price


def test_v2_extension_waits_for_reentry_instead_of_consuming(monkeypatch):
    from app.models.trading import ArmedSetup

    engine = EntryLifecycleV2(fast_confirm_enabled=True)
    now = int(time.time() * 1000)
    setup = ArmedSetup(
        setup_id="V2-reentry-test", symbol="REENTRY", strategy=Strategy.BREAKOUT_RETEST,
        direction=Direction.LONG, armed_at_ms=now - 5_000, expires_at_ms=now + 300_000,
        trigger_price=100.50, invalidation_price=98.80, stop_price=99.00, target_price=103.00,
        entry_zone_low=99.80, entry_zone_high=100.20, quality=90.0, risk_multiplier=1.0,
        timeframe="5m", metadata={
            "engine_version": "v2", "atr_value": 1.0, "signal_entry_price": 100.0,
            "structural_target_price": 103.0, "minimum_viable_rr": 0.80,
        },
    )
    monkeypatch.setattr("app.strategy.entry_engine_v2.time.time", lambda: now / 1000.0)

    extended = SimpleNamespace(
        bid=101.00, ask=101.02, bids=[(101.00, 80)] * 12, asks=[(101.02, 40)] * 12,
        orderbook_valid=True, quote_received_ms=now - 100, timeframes={},
    )
    status, intent, trace = engine.trigger(setup, extended, "d-extended")
    assert status == "pending"
    assert intent is None
    assert trace["reason"] == "setup_extended_wait_reentry"
    assert engine.consumed_record(setup.setup_id) is None

    # Price returns to trigger geometry while the setup is still valid. The live
    # microstructure path can execute without waiting another closed 1m candle.
    reentry = SimpleNamespace(
        bid=100.49, ask=100.50, bids=[(100.49, 80)] * 12, asks=[(100.50, 40)] * 12,
        orderbook_valid=True, quote_received_ms=now - 100, timeframes={},
    )
    status, intent, trace = engine.trigger(setup, reentry, "d-reentry")
    assert status == "triggered", trace
    assert intent is not None
    assert trace["confirmation_mode"] == "live_microstructure"
    assert intent.metadata["micro_confirmation"]["mode"] == "live_microstructure"


def test_v2_live_micro_confirmation_requires_fresh_quote(monkeypatch):
    from app.models.trading import ArmedSetup

    engine = EntryLifecycleV2(fast_confirm_enabled=True)
    now = int(time.time() * 1000)
    setup = ArmedSetup(
        setup_id="V2-live-stale", symbol="LIVE", strategy=Strategy.LIQUIDITY_SWEEP,
        direction=Direction.LONG, armed_at_ms=now - 5_000, expires_at_ms=now + 300_000,
        trigger_price=100.20, invalidation_price=98.80, stop_price=99.00, target_price=102.50,
        entry_zone_low=99.90, entry_zone_high=100.10, quality=90.0, risk_multiplier=.8,
        timeframe="5m", metadata={
            "engine_version": "v2", "atr_value": 1.0, "signal_entry_price": 100.0,
            "structural_target_price": 102.5, "minimum_viable_rr": 0.75,
        },
    )
    monkeypatch.setattr("app.strategy.entry_engine_v2.time.time", lambda: now / 1000.0)
    stale = SimpleNamespace(
        bid=100.19, ask=100.20, bids=[(100.19, 80)] * 12, asks=[(100.20, 40)] * 12,
        orderbook_valid=True, quote_received_ms=now - 10_000, timeframes={},
    )
    status, intent, trace = engine.trigger(setup, stale, "d-stale")
    assert status == "pending"
    assert intent is None
    assert trace["reason"] == "waiting_closed_1m_confirmation"
