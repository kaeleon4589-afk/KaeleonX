"""1E engine repair: post-arm momentum + directional revalidation.

This changes the V2 trigger itself, not a final risk or exposure policy.
"""
import time
from types import SimpleNamespace
from dataclasses import replace

from app.models.enums import Direction, Strategy
from app.models.market import Candle
from app.models.trading import ArmedSetup
from app.strategy.entry_engine_v2 import EntryLifecycleV2


def _setup(now, direction=Direction.SHORT):
    short = direction == Direction.SHORT
    return ArmedSetup(
        setup_id=f"strk-{direction.value}", symbol="STRK", strategy=Strategy.BREAKOUT_RETEST,
        direction=direction, armed_at_ms=now-240000, expires_at_ms=now+240000,
        trigger_price=0.07873, invalidation_price=0.0800 if short else 0.076,
        stop_price=0.07935818 if short else 0.07768,
        target_price=0.07768126 if short else 0.080,
        entry_zone_low=0.07864, entry_zone_high=0.07980,
        quality=88.16, risk_multiplier=1.0, timeframe="5m",
        metadata={"engine_version":"v2", "atr_value":0.00074,
                  "minimum_viable_rr":0.9, "signal_entry_price":0.07873},
    )


def _snapshot(now, price, close, open_price, *, candles=None):
    micro = Candle(now-60000,open_price,max(open_price,close)+0.000015,
                   min(open_price,close)-0.000015,close, 100)
    return SimpleNamespace(
        timeframes={"1m":[micro],**(candles or {})},
        bid=price, ask=price+0.00001,
        bids=[(price,100)]*12, asks=[(price+0.00001,100)]*12,
        quote_received_ms=now-30, orderbook_valid=True,
    )


def test_strk_short_rebound_after_bearish_confirm_cannot_trigger(monkeypatch):
    now = int(time.time()*1000)
    monkeypatch.setattr("app.strategy.entry_engine_v2.time.time",lambda: now/1000)
    engine = EntryLifecycleV2(fast_confirm_enabled=False)
    setup = _setup(now)
    # Captured from 1E: candle closed at 0.07847, executable SHORT near 0.07866.
    snap = _snapshot(now,0.07866,0.07847,0.07860)
    status, intent, trace = engine.trigger(setup,snap,'strk')
    assert status=='pending' and intent is None, trace
    assert trace['reason']=='breakout_live_confirmation_diverged'
    assert trace['adverse_from_close_atr']>0.20
    # A genuinely renewed downward impulse may still trigger in the same setup.
    snap.bid=0.07855
    snap.ask=0.07856
    snap.bids=[(snap.bid,100)]*12
    snap.asks=[(snap.ask,100)]*12
    status, intent, trace=engine.trigger(setup,snap,'reclaimed')
    assert status=='triggered' and intent is not None, trace


def test_long_rebound_guard_is_symmetric(monkeypatch):
    now=int(time.time()*1000)
    monkeypatch.setattr("app.strategy.entry_engine_v2.time.time",lambda: now/1000)
    setup=replace(_setup(now,Direction.LONG),trigger_price=0.07850,
                  stop_price=0.07790,target_price=0.0800,
                  invalidation_price=0.0770)
    snap=_snapshot(now,0.07865,0.07890,0.07870)
    status,intent,trace=EntryLifecycleV2(fast_confirm_enabled=False).trigger(setup,snap,'long')
    assert status=='pending' and intent is None,trace
    assert trace['reason']=='breakout_live_confirmation_diverged'


def _trending(count, descending,tf_ms):
    out=[]
    for i in range(count):
        close=(100.0-i*.1) if descending else (100.0+i*.1)
        out.append(Candle(i*tf_ms,close+.01,close+.03,close-.03,close,100))
    return out


def test_armed_setup_cancelled_if_two_timeframes_reverse(monkeypatch):
    now=int(time.time()*1000)
    monkeypatch.setattr("app.strategy.entry_engine_v2.time.time",lambda: now/1000)
    setup=_setup(now,Direction.SHORT)
    opposing={'5m':_trending(210,False,300000),'15m':_trending(120,False,900000)}
    snap=_snapshot(now,0.07855,0.07847,0.07860,candles=opposing)
    engine=EntryLifecycleV2(fast_confirm_enabled=False)
    status,intent,trace=engine.trigger(setup,snap,'opposite')
    assert status=='cancelled' and intent is None,trace
    assert trace['reason']=='breakout_regime_reversed'
    assert len(trace['opposing_timeframes'])>=2
    assert engine.consumed_record(setup.setup_id) is not None


def test_liquidity_sweep_is_not_subject_to_breakout_only_checks(monkeypatch):
    now=int(time.time()*1000)
    monkeypatch.setattr("app.strategy.entry_engine_v2.time.time",lambda: now/1000)
    setup=replace(_setup(now),strategy=Strategy.LIQUIDITY_SWEEP)
    snap=_snapshot(now,0.07866,0.07847,0.07860,
                   candles={'5m':_trending(210,False,300000),'15m':_trending(120,False,900000)})
    status,intent,trace=EntryLifecycleV2(fast_confirm_enabled=False).trigger(setup,snap,'sweep')
    assert trace.get('reason') not in {'breakout_regime_reversed','breakout_live_confirmation_diverged'}
