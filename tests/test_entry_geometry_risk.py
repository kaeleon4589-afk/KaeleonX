"""Regression of ONDO/XPL execution drift and realistic loss budget."""
import asyncio
from types import SimpleNamespace

import pytest

from app.models.enums import Direction, Strategy
from app.models.trading import TradeIntent
from app.risk.manager import RiskManager
from app.execution.paper import PaperExecutionEngine
from test_core_pipeline_refactor import (build_orchestrator, snapshot,
                                         PendingLiveExecution, CapturingAudit)


@pytest.mark.parametrize('mode,close,quote,stop,target,planned', [
    ('demo', .5580, .556255, .555000, .563446, .005376),
    ('live', .5580, .556255, .555000, .563446, .005376),
    ('demo', .11985, .11965, .119273, .121099, .0048),
])
def test_short_stop_from_old_candle_does_not_open_trade(mode, close, quote, stop, target, planned):
    audit = CapturingAudit()
    execution = PaperExecutionEngine(initial_equity=100) if mode == 'demo' else PendingLiveExecution()
    orchestrator, manager, _, _, _ = build_orchestrator(execution, mode=mode, audit=audit)
    def intent(*args, **kwargs):
        return TradeIntent('d', 'ONDO', Strategy.BREAKOUT_RETEST, Direction.LONG,
                           close, stop, target, 85, 1, '5m', metadata={'sl_pct': planned})
    orchestrator.router.evaluate = intent
    result = asyncio.run(orchestrator.on_snapshot(snapshot('ONDO', quote), 100, user_id='u1'))
    assert result is None
    assert not manager.positions
    assert not getattr(execution, 'submit_calls', [])
    assert orchestrator.last_rejection == 'entry_too_close_to_stop'
    assert any(event == 'SIGNAL_REJECTED' and data.get('reason') == 'entry_too_close_to_stop'
               for event, _, data in audit.events)


def test_full_configured_margin_with_entry_fee_reserve_for_long_and_short():
    for side, stop in [(Direction.LONG, 99.55), (Direction.SHORT, 100.45)]:
        intent = TradeIntent('d', 'BTC', Strategy.BREAKOUT_RETEST, side,
                             100, stop, 101 if side == Direction.LONG else 99,
                             85, 1, '5m')
        decision = RiskManager(.01, 10, fee_rate=.0006, exit_slippage_bps=2).evaluate(intent, 100, 10)
        assert decision.approved
        assert decision.margin_required + decision.quantity * .0006 <= 100
        assert decision.margin_required > 99
        assert decision.quantity > 990
        with_spare_wallet = RiskManager(.01, 10, fee_rate=.0006).evaluate(
            intent, 50, 10, available_equity=100)
        assert with_spare_wallet.quantity == 500
        assert with_spare_wallet.margin_required == 50


def test_breakout_keeps_structural_stop_and_rejects_weak_target(monkeypatch):
    import app.strategy.breakout_retest as breakout
    monkeypatch.setattr(breakout, 'candle_quality', lambda *a: (True, {}))
    def tf(candles):
        count = len(candles)
        return {'c':[100.0]*count,'o':[100.0]*count,'h':[101.0]*count,
                'l':[99.0]*count,'atr':1.0,'adx':30.0,
                'ema20':[99.5]*count,'ema50':[99.0]*count}
    monkeypatch.setattr(breakout, '_tf_values', tf)
    monkeypatch.setattr(breakout, '_bias', lambda *a, **k: ('long', {'adx':30.0,'stack_spread':.001}))
    monkeypatch.setattr(breakout, '_trigger', lambda *a, **k: (True, 'OK', {'extension_atr':0.0}))
    regime = SimpleNamespace(hard_block=False, breakout_allowed=True)
    frames = {'5m':[object()]*260,'15m':[object()]*200,'1h':[object()]*200}
    strategy = breakout.BreakoutRetestStrategy()
    assert strategy.evaluate(regime, frames['5m'], 'd', 'BTC', '5m',
                             snapshot=SimpleNamespace(timeframes=frames)) is None
    assert strategy.last_trace['reason'] == 'rr_too_low'
    assert strategy.last_trace['execution_rr'] < breakout.MIN_RR_TO_SIGNAL


def test_breakout_target_comes_from_swing_or_measured_range():
    from app.strategy.breakout_retest import _structure_target
    highs = [101.5] * 35 + [101.0]
    lows = [99.0] * 36
    assert _structure_target(Direction.LONG, 100, highs, lows, [102] * 26, lows) == 101.5
    assert _structure_target(Direction.LONG, 103, highs, lows, [102] * 26, lows) == 105.5


def test_sweep_front_runs_liquidity_target_and_keeps_sweep_extreme_stop(monkeypatch):
    import app.strategy.liquidity_sweep as sweep
    monkeypatch.setattr(sweep, 'candle_quality', lambda *a: (True, {}))
    monkeypatch.setattr(sweep, 'extract', lambda _: ([100]*260, [101]*260,
                                                    [99]*260, [100]*260, [1]*260))
    monkeypatch.setattr(sweep, 'atr', lambda *a: .5)
    candidate = dict(direction='long', score=85, stop_price=98.8,
                     target_level=105, rr_estimate=4, liquidity_level=99,
                     sweep_depth_atr=.5, sweep_wick_ratio=.5, sweep_rvol=1,
                     trigger_rvol=1, trigger_body_ratio=.5, trigger_close_pos=.8,
                     bars_since_sweep=2)
    monkeypatch.setattr(sweep, '_detect', lambda direction, **kw:
                        candidate if direction == 'long' else None)
    regime = SimpleNamespace(hard_block=False, sweep_allowed=True, risk_multiplier=.8)
    intent = sweep.LiquiditySweepStrategy().evaluate(
        regime, [object()]*260, 'd', 'BTC', '5m')
    assert intent.stop_price == 98.8
    assert intent.metadata['structural_target_price'] == 105
    assert intent.metadata['target_front_run_ratio'] == pytest.approx(.92)
    assert intent.target_price == pytest.approx(104.6)
    assert intent.metadata['sl_pct'] == pytest.approx(.012)
    assert intent.metadata['structural_tp_pct'] == pytest.approx(.05)
    assert intent.metadata['tp_pct'] == pytest.approx(.046)


@pytest.mark.parametrize('side,stop,observed', [
    (Direction.LONG, .555, .5548),
    (Direction.SHORT, .565, .5652),
])
def test_exit_uses_executable_quote_and_records_stop_gap(side, stop, observed):
    from app.models.trading import Position
    from app.position.manager import PositionManager
    from app.position.exit_engine import ExitEngine
    from app.telegram.notifications import TelegramTradeNotifier
    manager = PositionManager(ExitEngine())
    manager.exit_slippage_bps = 2
    entry = .56
    position = Position('p','d','ONDO',side,100,entry,stop,
                        .57 if side == Direction.LONG else .55)
    manager.add(position, persist=False)
    kwargs = {'bid': observed} if side == Direction.LONG else {'ask': observed}
    manager.mark('ONDO', observed, 123_000, quote_received_ms=120_500, **kwargs)
    assert position.status == 'CLOSED'
    assert position.closed_at == 120_500
    assert position.exit_quote_delay_ms == 2500
    assert position.exit_trigger_price == observed
    assert position.stop_gap_bps > 0
    assert position.exit_price < observed if side == Direction.LONG else position.exit_price > observed

    sent = []
    notifier = object.__new__(TelegramTradeNotifier)
    notifier._schedule = lambda uid, msg: sent.append(msg)
    notifier.position_closed('u', 'demo', position)
    assert 'Stop Loss:' in sent[0] and 'Diferencia frente al SL:' in sent[0]


def test_monitor_quote_only_visits_owner_of_open_risk():
    from app.trading.runtime import UserTradingRuntimeManager
    from app.models.trading import Position
    calls = []
    class Execution:
        async def get_equity(self): return 100
    class Orchestrator:
        pending_execution = None
        def __init__(self, uid): self.uid = uid
        async def on_snapshot(self, snapshot, capital, **kwargs):
            calls.append(self.uid)
            return None
    def runtime(uid, held):
        positions = {'p': Position('p','d','ONDO',Direction.LONG,1,100,99,102)} if held else {}
        return SimpleNamespace(user_id=uid, mode='live', execution=Execution(),
                               configured_capital=20, trading_enabled=True,
                               coinw_verified=True, live_allowed=True,
                               position_manager=SimpleNamespace(positions=positions,
                                   consume_state_changed=lambda: False),
                               orchestrator=Orchestrator(uid))
    manager = object.__new__(UserTradingRuntimeManager)
    manager._runtimes = {'owner': runtime('owner', True), 'bystander': runtime('bystander', False)}
    manager.settings = SimpleNamespace(min_operating_capital=3)
    manager.audit = CapturingAudit()
    async def noop(*args, **kwargs): pass
    manager.refresh = noop
    manager._persist_state = noop
    asyncio.run(manager._run_snapshot(SimpleNamespace(symbol='ONDO', last=None,
                                                      monitor_only=True)))
    assert calls == ['owner']


def test_short_entry_moving_toward_stop_is_rejected():
    audit = CapturingAudit()
    execution = PaperExecutionEngine(initial_equity=100)
    orchestrator, manager, _, _, _ = build_orchestrator(execution, mode='demo', audit=audit)
    def signal(*args, **kwargs):
        return TradeIntent('d', 'BTC', Strategy.LIQUIDITY_SWEEP, Direction.SHORT,
                           100.0, 100.5, 99.4, 85, 1, '5m', metadata={'sl_pct': .005})
    orchestrator.router.evaluate = signal
    result = asyncio.run(orchestrator.on_snapshot(snapshot('BTC', 100.35), 100, user_id='u1'))
    assert result is None and not manager.positions
    assert orchestrator.last_rejection == 'entry_too_close_to_stop'


def test_chasing_price_far_from_candle_is_rejected_before_order():
    audit = CapturingAudit()
    execution = PaperExecutionEngine(initial_equity=100)
    orchestrator, manager, _, _, _ = build_orchestrator(execution, mode='demo', audit=audit)
    def signal(*args, **kwargs):
        return TradeIntent('d', 'BTC', Strategy.BREAKOUT_RETEST, Direction.LONG,
                           100.0, 99.5, 100.8, 85, 1, '5m', metadata={'sl_pct': .005})
    orchestrator.router.evaluate = signal
    assert asyncio.run(orchestrator.on_snapshot(snapshot('BTC', 100.20), 100, user_id='u1')) is None
    assert not manager.positions
    assert orchestrator.last_rejection == 'entry_far_from_signal'


@pytest.mark.parametrize('side,stop,target', [
    (Direction.LONG, 99.5, 100.8),
    (Direction.SHORT, 100.5, 99.2),
])
def test_valid_fresh_setups_still_execute(side, stop, target):
    execution = PaperExecutionEngine(initial_equity=100)
    orchestrator, manager, _, _, _ = build_orchestrator(execution, mode='demo')
    def signal(regime, candles, decision_id, symbol, timeframe, current_price=None, **kwargs):
        return TradeIntent(decision_id, symbol, Strategy.BREAKOUT_RETEST, side,
                           100, stop, target, 85, 1, timeframe, metadata={'sl_pct':.005})
    orchestrator.router.evaluate = signal
    result = asyncio.run(orchestrator.on_snapshot(snapshot('BTC', 100), 100, user_id='u1'))
    assert result and result['filled'] is True
    assert len(manager.positions) == 1
