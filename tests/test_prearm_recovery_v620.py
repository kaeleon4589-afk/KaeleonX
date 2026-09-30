from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.execution.paper import PaperExecutionEngine
from app.models.enums import Direction, Strategy
from app.models.market import Candle
from app.risk.manager import RiskManager
from app.strategy import armed_entry, breakout_retest
from app.strategy.armed_entry import ArmedEntryEngine
from app.strategy.prearm_policy import prearm_policy
from app.strategy.router import StrategyRouter
from test_armed_entry_v6 import (
    _breakout_precursor_snapshot, _breakout_retest_snapshot,
    _sweep_precursor_snapshot, _range_sweep_snapshot,
)


@pytest.fixture(autouse=True)
def recovery(monkeypatch):
    monkeypatch.setenv("TRADE_PREARM_PROFILE", "recovery")


def context(side="long", active="TREND_CONTINUATION", hard_block=False):
    regime = SimpleNamespace(
        hard_block=hard_block, breakout_allowed=active == "TREND_CONTINUATION",
        sweep_allowed=False, risk_multiplier=.65,
        direction=Direction.BULLISH if side == "long" else Direction.BEARISH,
    )
    return regime, {"active": active, "features": {"trend_bias": side}}


def reflect(snap):
    def flip(c):
        return Candle(c.timestamp, 200-c.open, 200-c.low, 200-c.high, 200-c.close, c.volume)
    frames = {key: [flip(c) for c in candles] for key, candles in snap.timeframes.items()}
    return SimpleNamespace(**{**vars(snap), "candles": frames["5m"], "timeframes": frames,
                              "bid": 200-snap.ask, "ask": 200-snap.bid, "last": 200-snap.last})


@pytest.mark.parametrize("side", ["long", "short"])
def test_recovery_low_volume_breakout_watch_to_arm_to_trigger_to_paper_fill(monkeypatch, side):
    # Real strategy and PaperExecutionEngine, synthetic candles; no exchange connection.
    precursor = _breakout_precursor_snapshot()
    retest = _breakout_retest_snapshot()
    for snap in (precursor, retest):
        bar = snap.timeframes["5m"][259]
        snap.timeframes["5m"][259] = Candle(bar.timestamp, bar.open, bar.high, bar.low, bar.close, 80)
    if side == "short":
        precursor, retest = reflect(precursor), reflect(retest)
    regime, meta = context(side, "RANGE")
    engine = ArmedEntryEngine()
    monkeypatch.setenv("TRADE_PREARM_PROFILE", "strict")
    assert engine.discover_watch(regime, precursor, "BR", "5m", meta) is None
    # Independently prove the reduced volume gate (even outside RANGE).
    trend_regime, trend_meta = context(side)
    engine._discover_breakout_watch(trend_regime, precursor, "BR", "5m", trend_meta)
    assert engine.watch_trace["breakout"]["reason"] == "breakout_not_detected"
    monkeypatch.setenv("TRADE_PREARM_PROFILE", "recovery")
    watch = engine.discover_watch(regime, precursor, "BR", "5m", meta)
    assert watch is not None and watch.strategy == Strategy.BREAKOUT_RETEST
    status, setup, trace = engine.advance_watch(watch, retest)
    assert status == "armed", trace
    sign = 1 if side == "long" else -1
    trigger = setup.trigger_price
    now = setup.armed_at_ms + 120_000
    monkeypatch.setattr(armed_entry, "_now_ms", lambda: now)
    micro = [Candle(now - (30-i)*60_000, trigger, trigger+.03, trigger-.03, trigger, 100) for i in range(30)]
    opened, close = trigger-sign*.06, trigger+sign*.03
    micro[-1] = Candle(now-60_000, opened, max(opened,close)+.005, min(opened,close)-.005, close, 200)
    quote = trigger + sign*.01
    confirmation = SimpleNamespace(**{**vars(retest), "bid": quote-.001, "ask": quote+.001,
                                     "timeframes": {**retest.timeframes, "1m": micro}})
    status, intent, trace = engine.trigger(setup, confirmation, "recovery-test")
    assert status == "triggered", trace
    assert intent.direction == (Direction.LONG if side == "long" else Direction.SHORT)
    assert trace["execution_rr"] >= 1.1
    risk = RiskManager(max_leverage=10).evaluate(intent, equity=3, available_equity=100, leverage=10)
    assert risk.approved and risk.quantity == 30
    result = PaperExecutionEngine(initial_equity=100, leverage=10).submit(
        intent, quantity=risk.quantity, market={"bid": confirmation.bid, "ask": confirmation.ask, "ts": now})
    assert result["filled"] is True, result
    # Same setup cannot trigger a second order.
    status, duplicate, _ = engine.trigger(setup, confirmation, "duplicate")
    assert duplicate is None and status == "cancelled"


@pytest.mark.parametrize("side", ["long", "short"])
def test_local_liquidity_pool_replaces_distant_34_bar_extreme(monkeypatch, side):
    snap = _sweep_precursor_snapshot()
    snap.candles[235] = Candle(235*300_000, 100.1, 101, 95, 100.2, 100)
    if side == "short":
        snap = reflect(snap)
    regime, meta = context(side)
    engine = ArmedEntryEngine()
    monkeypatch.setenv("TRADE_PREARM_PROFILE", "strict")
    assert engine._discover_sweep_watch(regime, snap, "SW", "5m", meta) is None
    assert engine.watch_trace["sweep"]["reason"] == "sweep_level_not_reached"
    monkeypatch.setenv("TRADE_PREARM_PROFILE", "recovery")
    watch = engine._discover_sweep_watch(regime, snap, "SW", "5m", meta)
    assert watch is not None
    assert watch.metadata["liquidity_level"] == 100
    assert watch.metadata["liquidity_lookback_bars"] == 12


@pytest.mark.parametrize("active", ["TREND_CONTINUATION", "RANGE", "VOLATILE_SWEEP", "UNKNOWN"])
def test_hard_block_prevents_all_discovery(active):
    regime, meta = context(active=active, hard_block=True)
    engine = ArmedEntryEngine()
    assert engine.discover(regime, _range_sweep_snapshot(), "SW", "5m", meta) is None
    assert engine.discover_watch(regime, _breakout_precursor_snapshot(), "BR", "5m", meta) is None


def test_neutral_and_conflicting_direction_remain_blocked():
    regime, meta = context(active="RANGE")
    regime.direction = Direction.NEUTRAL
    meta["features"]["trend_bias"] = "neutral"
    engine = ArmedEntryEngine()
    assert engine.discover_watch(regime, _breakout_precursor_snapshot(), "BR", "5m", meta) is None
    regime.direction = Direction.BULLISH
    meta["features"]["trend_bias"] = "short"
    assert engine.discover(regime, _range_sweep_snapshot(), "SW", "5m", meta) is None


@pytest.mark.parametrize("side", ["long", "short"])
def test_single_htf_conflict_passes_discovery_and_promotion_both_opposed_block(monkeypatch, side):
    precursor, retest = _breakout_precursor_snapshot(), _breakout_retest_snapshot()
    if side == "short":
        precursor, retest = reflect(precursor), reflect(retest)
    opposite = "short" if side == "long" else "long"
    regime, meta = context(side)
    monkeypatch.setattr(breakout_retest, "_bias", lambda tf, adx_min: (opposite if adx_min == breakout_retest.H1_ADX_MIN else "none", {}))
    engine = ArmedEntryEngine()
    watch = engine._discover_breakout_watch(regime, precursor, "BR", "5m", meta)
    assert watch is not None
    status, setup, trace = engine.advance_watch(watch, retest)
    assert status == "armed", trace
    assert engine._discover_breakout(regime, retest, "BR", "5m", meta) is not None
    monkeypatch.setattr(breakout_retest, "_bias", lambda *args, **kwargs: (opposite, {}))
    assert engine._discover_breakout_watch(regime, precursor, "BR", "5m", meta) is None
    assert engine._discover_breakout(regime, retest, "BR", "5m", meta) is None
    status, setup, trace = engine.advance_watch(watch, retest)
    assert status == "cancelled" and trace["reason"] == "mtf_bias_conflict_with_watch"


def test_profile_defaults_and_invalid_value_fail_closed(monkeypatch):
    monkeypatch.delenv("TRADE_PREARM_PROFILE")
    assert prearm_policy().name == "recovery"
    monkeypatch.setenv("TRADE_PREARM_PROFILE", "typo")
    assert prearm_policy().name == "strict"


def test_recovery_wick_tolerance_requires_alignment():
    diag = {"trend_context_status": "ok", "current_adx": 16,
            "current_ema_alignment": .75, "current_ema_alignment_edge": .5}
    value, trace = armed_entry._adaptive_retest_penetration_limit(Direction.LONG, 2, diag)
    assert value == .5 and trace["retest_penetration_mode"] == "recovery_aligned"
    value, _ = armed_entry._adaptive_retest_penetration_limit(Direction.SHORT, 2, diag)
    assert value == .3


def test_router_reports_both_branch_rejections():
    router = StrategyRouter()
    regime, meta = context()
    snap = _breakout_precursor_snapshot()
    snap.timeframes["5m"][-1] = Candle(259*300_000, 100, 100.6, 99.4, 100, 100)
    router.discover_watch(regime, snap, "BR", "5m", meta)
    assert "breakout" in router.last_trace["rejection_reasons"]
    assert "sweep" in router.last_trace["rejection_reasons"]
    assert router.last_trace["prearm_profile"] == "recovery"
