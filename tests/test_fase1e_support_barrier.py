"""1E correction: support/resistance runway, no overnight pauses or 1F filters."""
import asyncio
from types import SimpleNamespace

from app.models.enums import Direction, Strategy
from app.models.market import Candle
from app.models.trading import TradeIntent
from app.strategy.entry_barrier_f1e import Zone, build_context, evaluate_barrier


def check(direction, zone, *, strategy=Strategy.BREAKOUT_RETEST,
          entry=100, stop=None, target=None):
    stop = (101 if direction == Direction.SHORT else 99) if stop is None else stop
    target = (98 if direction == Direction.SHORT else 102) if target is None else target
    return evaluate_barrier(context={'zones': (zone,), 'sources': ('5m', '15m')},
                            direction=direction, strategy=strategy,
                            entry=entry, stop=stop, target=target, fee_rate=.0006)


def test_short_near_support_rejected_before_tp():
    reason, detail = check(Direction.SHORT, Zone(99.55, .12, 'support', '15m', 2))
    assert reason == 'support_too_close'
    assert detail['barrier_timeframe'] == '15m'
    assert detail['barrier_free_net_rr'] < 1


def test_long_near_resistance_rejected_before_tp():
    reason, detail = check(Direction.LONG, Zone(100.45, .10, 'resistance', '1h', 1))
    assert reason == 'resistance_too_close'
    assert detail['barrier_free_net_rr'] < 1


def test_if_price_inside_support_zone_short_rejected():
    # Support center slightly ABOVE entry: bottom of band surrounds entry.
    assert check(Direction.SHORT, Zone(100.03, .08, 'support', '5m', 2))[0] == 'support_too_close'


def test_distant_barrier_and_broken_support_not_blocked():
    assert check(Direction.SHORT, Zone(97, .1, 'support', '15m', 2))[0] is None
    assert check(Direction.LONG, Zone(103, .1, 'resistance', '15m', 2))[0] is None
    assert check(Direction.SHORT, Zone(100.4, .1, 'support', '15m', 2))[0] is None


def test_liquidity_sweep_not_affected():
    assert check(Direction.SHORT, Zone(99.7, .12, 'support', '1h', 1),
                 strategy=Strategy.LIQUIDITY_SWEEP)[0] is None


def test_missing_structure_does_not_dry_market():
    reason, detail = evaluate_barrier(context=None, direction=Direction.SHORT,
                                      strategy=Strategy.BREAKOUT_RETEST,
                                      entry=100, stop=101, target=98)
    assert reason is None
    assert detail['barrier_check'] == 'no_confirmed_obstacle'


def test_only_closed_candles_in_barrier_context():
    now = 1_800_000_000_000
    rows = [Candle(now - 41*300_000 + i*300_000, 100, 101, 99, 100, 10)
            for i in range(40)]
    forming = Candle(now - 100_000, 100, 9999, .001, 9999, 20)
    a = build_context(SimpleNamespace(timeframes={'5m': rows+[forming]}), now_ms=now)
    b = build_context(SimpleNamespace(timeframes={'5m': rows}), now_ms=now)
    assert a == b


def test_actual_strk_execution_rr_would_not_resolve_support_proximity():
    # Only illustrative pivot (not proven historical STRK zone from the log):
    # the log does not contain actual closed-candle support levels.
    e, sl, tp = 0.07863427, 0.07935818059341936, 0.07768125689580549
    reason, _ = evaluate_barrier(context={'zones': (Zone(.0784, .00015, 'support', '15m', 2),),
                                                  'sources': ('5m', '15m')},
                                 direction=Direction.SHORT, strategy=Strategy.BREAKOUT_RETEST,
                                 entry=e, stop=sl, target=tp)
    assert reason == 'support_too_close'


def test_integration_reject_before_risk_on_confirmed_zone():
    from app.execution.paper import PaperExecutionEngine
    from test_core_pipeline_refactor import build_orchestrator, snapshot, CapturingAudit
    audit = CapturingAudit()
    execution = PaperExecutionEngine(initial_equity=100)
    engine, manager, *_ = build_orchestrator(execution, mode='demo', audit=audit)
    engine.router.evaluate = lambda *args, **kwargs: TradeIntent(
        'd', 'BTC', Strategy.BREAKOUT_RETEST, Direction.SHORT, 100, 101, 98,
        85, 1, '5m', metadata={'engine_version': 'v2', 'sl_pct': .01, 'atr_value': 1.0})
    # Fixture snapshot defaults to bid/ask around 100, preserving valid net RR.
    snap = snapshot('BTC', 100)
    snap.structural_context_f1e = {'zones': (Zone(99.5, .12, 'support', '1h', 2),),
                                    'sources': ('5m', '1h')}
    result = asyncio.run(engine.on_snapshot(snap, 100, user_id='u1'))
    assert result is None
    assert engine.last_rejection == 'support_too_close'
    assert not manager.positions
    assert any(name == 'SIGNAL_REJECTED' and data.get('reason') == 'support_too_close'
               for name, _, data in audit.events)
