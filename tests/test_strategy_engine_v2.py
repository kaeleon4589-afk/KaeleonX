from __future__ import annotations

from types import SimpleNamespace
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
    assert candidate.metadata["execution_rr"] >= 1.05


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
    assert candidate.metadata["execution_rr"] >= 1.05


def test_v2_armed_entry_requires_micro_confirmation_and_is_single_use(monkeypatch):
    regime = SimpleNamespace(hard_block=False, breakout_allowed=True, sweep_allowed=False,
                             direction=Direction.LONG, risk_multiplier=1.0)
    snap = breakout_snapshot()
    candidate = BreakoutRetestStrategyV2().scan(regime, snap)
    assert candidate and candidate.stage == "READY"
    engine = EntryLifecycleV2(fast_confirm_enabled=False)
    setup = engine.arm(candidate, "TEST", "5m")
    assert setup is not None

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
    assert status == "triggered", trace
    assert intent is not None
    assert intent.metadata["engine_version"] == "v2"
    assert intent.metadata["execution_rr"] >= 1.05

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
    assert regime.breakout_allowed is True
    assert regime.sweep_allowed is False
    assert regime.direction == Direction.LONG
