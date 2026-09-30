"""Synthetic regression and complete watch -> order tests, no exchange calls."""
import asyncio
from dataclasses import replace

import pytest

from app.config.settings import Settings
from app.execution.paper import PaperExecutionEngine
from app.models.market import Candle
from app.strategy.armed_entry import ArmedEntryEngine
from app.strategy.liquidity_sweep import continuation_confirmation
from app.strategy.prearm_policy import RECOVERY, STRICT
from app.strategy.router import StrategyRouter
from test_armed_entry_v6 import _sweep_precursor_snapshot, _sweep_reclaim_snapshot
from test_prearm_recovery_v620 import context, reflect
from test_core_pipeline_refactor import build_orchestrator
from test_execution_quote_refresh_v621 import quote, drain
from test_armed_priority_monitor import RegimeMustNotRun


@pytest.mark.parametrize('side', ['long', 'short'])
def test_recovery_watch_reclaim_micro_risk_submit_fill(monkeypatch, side):
    async def run():
        monkeypatch.setenv('TRADE_PREARM_PROFILE', 'recovery')
        engine, manager, audit, _, _ = build_orchestrator(PaperExecutionEngine(initial_equity=100, leverage=10))
        engine.entry_min_stop_atr = Settings().trade_entry_min_stop_atr
        router = engine.router = StrategyRouter()
        engine.regime_engine = RegimeMustNotRun()
        precursor, snap = _sweep_precursor_snapshot(), _sweep_reclaim_snapshot()
        # Modest volume that previously failed before arming.
        bar = snap.candles[-1]
        snap.candles[-1] = Candle(bar.timestamp, bar.open, bar.high, bar.low, bar.close, 60)
        if side == 'short': precursor, snap = reflect(precursor), reflect(snap)
        regime, meta = context(side)
        watch = router.discover_watch(regime, precursor, 'SW', '5m', meta)
        assert watch is not None
        engine.watching_setups['SW'] = watch
        snap.monitor_only = snap.watch_monitor = True
        await engine.on_snapshot(snap, 3, user_id='u1', available_equity=100)
        await drain(engine)
        assert 'SW' in engine.armed_setups, engine.last_rejection
        setup = engine.armed_setups['SW']
        assert setup.metadata['sweep_confirmation_mode'] == 'closed_5m_reclaim'
        assert setup.metadata['sweep_5m_continuation_confirmed'] is False
        assert not manager.positions
        now = setup.armed_at_ms + 120000
        monkeypatch.setattr('app.orchestrator.time.time', lambda: now/1000)
        trigger, sign = setup.trigger_price, 1 if side == 'long' else -1
        micro = [Candle(now-(30-i)*60000, trigger, trigger+.03, trigger-.03, trigger, 100) for i in range(30)]
        snap.timeframes['1m'] = micro
        snap.watch_monitor = False
        snap.armed_monitor = True
        snap.quote_received_ms = now-23681
        async def refresh(symbol): return quote(symbol, price=trigger+sign*.01)
        engine.execution_quote_provider = refresh
        # Arming alone cannot place an order: neutral micro candles still wait.
        assert await engine.on_snapshot(snap, 3, user_id='u1', available_equity=100) is None
        assert engine.rejection_funnel.payload()['submitted'] == 0
        opened, close = trigger-sign*.06, trigger+sign*.03
        micro[-1] = Candle(now-60000, opened, max(opened, close)+.005, min(opened, close)-.005, close, 200)
        result = await engine.on_snapshot(snap, 3, user_id='u1', available_equity=100)
        await drain(engine)
        assert result and result['filled'], engine.last_rejection
        assert manager.positions
        funnel = engine.rejection_funnel.payload()
        assert funnel['armed'] == funnel['submitted'] == funnel['filled'] == 1
        assert any(event == 'SETUP_ARMED' for event, _, _ in audit.events)
        assert any(event == 'POSITION_OPENED' for event, _, _ in audit.events)
        await engine.on_snapshot(snap, 3, user_id='u1', available_equity=100)
        await drain(engine)
        assert engine.rejection_funnel.payload()['submitted'] == 1
    asyncio.run(run())


@pytest.mark.parametrize('side', ['long', 'short'])
@pytest.mark.parametrize('profile', ['strict', 'recovery'])
def test_same_closed_reclaim_profile_controls_both_paths(monkeypatch, side, profile):
    monkeypatch.setenv('TRADE_PREARM_PROFILE', profile)
    precursor, snap = _sweep_precursor_snapshot(), _sweep_reclaim_snapshot()
    if side == 'short': precursor, snap = reflect(precursor), reflect(snap)
    regime, meta = context(side)
    engine = ArmedEntryEngine()
    watch = engine.discover_watch(regime, precursor, 'SW', '5m', meta)
    assert watch is not None
    status, setup, trace = engine.advance_watch(watch, snap)
    direct = engine._discover_sweep(regime, snap, 'SW', '5m', meta)
    assert (status == 'armed') == (profile == 'recovery'), trace
    assert (direct is not None) == (profile == 'recovery')


@pytest.mark.parametrize('side', ['long', 'short'])
@pytest.mark.parametrize('bad', ['no_reclaim', 'no_volume', 'no_wick'])
def test_recovery_still_requires_real_sweep_reclaim(monkeypatch, side, bad):
    monkeypatch.setenv('TRADE_PREARM_PROFILE', 'recovery')
    precursor, snap = _sweep_precursor_snapshot(), _sweep_reclaim_snapshot()
    bar = snap.candles[-1]
    changes = {'no_reclaim': dict(close=99.6), 'no_volume': dict(volume=1),
               'no_wick': dict(open=99.56)}[bad]
    snap.candles[-1] = replace(bar, **changes)
    if side == 'short': precursor, snap = reflect(precursor), reflect(snap)
    regime, meta = context(side)
    engine = ArmedEntryEngine()
    watch = engine.discover_watch(regime, precursor, 'SW', '5m', meta)
    status, setup, _ = engine.advance_watch(watch, snap)
    assert setup is None and status != 'armed'
    assert engine._discover_sweep(regime, snap, 'SW', '5m', meta) is None


@pytest.mark.parametrize('side', ['long', 'short'])
def test_aster_observed_continuation_extension_passes_recovery_only(side):
    # OHLC/volume ratios from the logged first continuation, not full candle replay.
    o, h, l, c, v = [[.7687]*30 for _ in range(5)]
    v = [100]*30
    o[-1], h[-1], l[-1], c[-1], v[-1] = .7687, .7737, .7673, .7718, 99.3968
    level, atr = .765, (.7718-.765)/1.306855
    if side == 'short':
        o, h, l, c = [2-x for x in o], [2-x for x in l], [2-x for x in h], [2-x for x in c]
        level = 2-level
    kwargs = dict(sweep_idx=28, trigger_idx=29, o=o, h=h, l=l, c=c, v=v,
                  ema20=c, ema50=c, atr_value=atr, level=level)
    ok, reason, diag = continuation_confirmation(side, **kwargs, policy=STRICT)
    assert not ok and reason == 'sweep_continuation_too_extended'
    ok, _, diag = continuation_confirmation(side, **kwargs, policy=RECOVERY)
    assert ok, diag
    kwargs['atr_value'] = abs(c[-1]-level)/2.416696
    ok, reason, diag = continuation_confirmation(side, **kwargs, policy=RECOVERY)
    assert not ok and reason == 'sweep_continuation_too_extended'
    assert reason in diag['failed_checks']
