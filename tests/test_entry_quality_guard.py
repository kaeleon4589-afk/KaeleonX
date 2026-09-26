import asyncio
import time
from types import SimpleNamespace

from app.execution.paper import PaperExecutionEngine
from app.models.enums import Direction, Strategy
from app.models.trading import Position, TradeIntent
from app.strategy import breakout_retest as breakout
from app.strategy import liquidity_sweep as sweep
from test_core_pipeline_refactor import CapturingAudit, build_orchestrator, snapshot


def _intent(direction=Direction.LONG, *, entry=100.0, stop=99.0, target=102.0, atr=1.0):
    return TradeIntent(
        'd', 'BTC', Strategy.BREAKOUT_RETEST, direction,
        entry, stop, target, 90, 1.0, '5m',
        metadata={'sl_pct': abs(entry - stop) / entry, 'atr_value': atr, 'atr_pct': atr / entry},
    )


def test_breakout_requires_close_confirmation_not_only_wick_break():
    count = 260
    o = [100.0] * count
    h = [100.7] * count
    l = [99.0] * count
    c = [100.1] * count
    # Reset window reaches EMA20, but trigger only wicks above the prior high.
    h[-2] = 101.0
    l[-4] = 99.1
    o[-1] = 100.0
    h[-1] = 101.25
    l[-1] = 99.9
    c[-1] = 100.45
    tf = {
        'o': o, 'h': h, 'l': l, 'c': c,
        'ema20': [99.2] * count,
        'ema50': [98.8] * count,
        'atr': 1.0,
    }
    ok, reason, diag = breakout._trigger('long', tf)
    assert ok is False
    assert reason in {'NO_FRESH_CONFIRMATION', 'NO_FRESH_BREAKOUT_RETEST'}
    assert h[-1] > h[-2]  # wick did break the prior high
    assert c[-1] < h[-2] + breakout.BREAKOUT_CLOSE_BUFFER_ATR
    assert diag['trigger_body_ratio'] >= 0


def test_strategy_entry_thresholds_are_no_longer_permissive_defaults():
    assert breakout.MIN_SCORE_TO_SIGNAL >= 78
    assert breakout.H1_ADX_MIN >= 16
    assert breakout.M15_ADX_MIN >= 14
    assert breakout.M5_ADX_MIN >= 12
    assert breakout.ENTRY_MAX_STRUCTURE_EXTENSION_ATR <= 0.75
    assert breakout.MAX_IMPULSE_CONSUMED_RATIO <= 0.45
    assert breakout.RETEST_MAX_BARS_AFTER_BREAKOUT <= 3
    assert sweep.MIN_SCORE >= 78
    assert sweep.MIN_RR >= 1.05
    assert sweep.TRIGGER_EXTENSION_MAX_ATR <= 1.10
    assert sweep.TRIGGER_MIN_BODY_RATIO >= 0.24
    assert sweep.SWEEP_MAX_AGE_BARS <= 5


def test_execution_rejects_chased_entry_after_closed_candle_signal():
    audit = CapturingAudit()
    execution = PaperExecutionEngine(initial_equity=100)
    orchestrator, manager, _, _, _ = build_orchestrator(execution, mode='demo', audit=audit)
    orchestrator.router.evaluate = lambda *a, **k: _intent()
    result = asyncio.run(orchestrator.on_snapshot(snapshot('BTC', 100.30), 100, user_id='u1'))
    assert result is None
    assert not manager.positions
    assert orchestrator.last_rejection == 'entry_chased_after_signal'


def test_execution_rejects_confirmation_that_reversed_before_fill():
    audit = CapturingAudit()
    execution = PaperExecutionEngine(initial_equity=100)
    orchestrator, manager, _, _, _ = build_orchestrator(execution, mode='demo', audit=audit)
    orchestrator.router.evaluate = lambda *a, **k: _intent()
    result = asyncio.run(orchestrator.on_snapshot(snapshot('BTC', 99.80), 100, user_id='u1'))
    assert result is None
    assert not manager.positions
    assert orchestrator.last_rejection == 'entry_confirmation_lost_before_fill'


def test_execution_rejects_stop_inside_atr_and_spread_noise():
    audit = CapturingAudit()
    execution = PaperExecutionEngine(initial_equity=100)
    orchestrator, manager, _, _, _ = build_orchestrator(execution, mode='demo', audit=audit)
    orchestrator.router.evaluate = lambda *a, **k: _intent(stop=99.60, target=101.5)
    result = asyncio.run(orchestrator.on_snapshot(snapshot('BTC', 100.0), 100, user_id='u1'))
    assert result is None
    assert not manager.positions
    assert orchestrator.last_rejection == 'stop_inside_market_noise'


def test_execution_rejects_severe_orderbook_conflict():
    audit = CapturingAudit()
    execution = PaperExecutionEngine(initial_equity=100)
    orchestrator, manager, _, _, _ = build_orchestrator(execution, mode='demo', audit=audit)
    # Omit ATR metadata so this test isolates the final order-book veto.
    orchestrator.router.evaluate = lambda *a, **k: TradeIntent(
        'd', 'BTC', Strategy.BREAKOUT_RETEST, Direction.LONG,
        100.0, 99.0, 102.0, 90, 1.0, '5m', metadata={'sl_pct': 0.01},
    )
    snap = snapshot('BTC', 100.0)
    snap.bids = [(99.99 - i * 0.01, 1.0) for i in range(20)]
    snap.asks = [(100.01 + i * 0.01, 8.0) for i in range(20)]
    result = asyncio.run(orchestrator.on_snapshot(snap, 100, user_id='u1'))
    assert result is None
    assert not manager.positions
    assert orchestrator.last_rejection == 'orderbook_conflict'


def test_negative_close_starts_global_and_symbol_reentry_cooldowns():
    audit = CapturingAudit()
    execution = PaperExecutionEngine(initial_equity=100)
    orchestrator, manager, _, _, _ = build_orchestrator(execution, mode='demo', audit=audit)
    loss = Position('p-loss', 'd-loss', 'BTC', Direction.LONG, 1.0, 100.0, 99.0, 102.0)
    loss.status = 'CLOSED'
    loss.realized_pnl = -1.0
    loss.closed_at = int(time.time() * 1000)
    orchestrator.register_closed_position(loss)

    assert asyncio.run(orchestrator.on_snapshot(snapshot('ETH', 2000), 100, user_id='u1')) is None
    assert orchestrator.last_rejection == 'post_loss_global_cooldown'

    orchestrator.last_loss_at = time.time() - orchestrator.post_loss_global_cooldown_seconds - 1
    assert asyncio.run(orchestrator.on_snapshot(snapshot('BTC', 100), 100, user_id='u1')) is None
    assert orchestrator.last_rejection == 'post_loss_symbol_cooldown'


def test_seeded_loss_cooldown_survives_runtime_rebuild():
    execution = PaperExecutionEngine(initial_equity=100)
    orchestrator, _, _, _, _ = build_orchestrator(execution, mode='demo')
    orchestrator.seed_loss_cooldowns([{
        'status': 'CLOSED', 'symbol': 'BTC', 'closed_at': int(time.time() * 1000),
        'realized_pnl': -0.5, 'entry_fee': 0.0, 'exit_fee': 0.0, 'funding_pnl': 0.0,
    }])
    assert orchestrator.last_loss_at > 0
    assert orchestrator.last_symbol_loss_at['BTC'] > 0


def _synthetic_sweep_arrays(direction: str):
    n = 100
    o = [100.1] * n
    h = [101.0] * n
    l = [100.0] * n
    c = [100.2] * n
    v = [100.0] * n
    ema20 = [100.15] * n
    ema50 = [100.05] * n
    # Provide opposing liquidity for a valid structural target.
    if direction == 'long':
        h[70] = 103.0
        sweep_idx = 96
        o[sweep_idx], h[sweep_idx], l[sweep_idx], c[sweep_idx], v[sweep_idx] = 100.20, 100.30, 99.70, 100.15, 220.0
        for idx in (97, 98):
            o[idx], h[idx], l[idx], c[idx], v[idx] = 100.12, 100.45, 99.85, 100.28, 140.0
        o[99], h[99], l[99], c[99], v[99] = 100.10, 100.85, 100.00, 100.72, 210.0
    else:
        # Mirror around 100.0.
        h = [100.0] * n
        l = [99.0] * n
        o = [99.9] * n
        c = [99.8] * n
        ema20 = [99.85] * n
        ema50 = [99.95] * n
        l[70] = 97.0
        sweep_idx = 96
        o[sweep_idx], h[sweep_idx], l[sweep_idx], c[sweep_idx], v[sweep_idx] = 99.80, 100.30, 99.70, 99.85, 220.0
        for idx in (97, 98):
            o[idx], h[idx], l[idx], c[idx], v[idx] = 99.88, 100.15, 99.55, 99.72, 140.0
        o[99], h[99], l[99], c[99], v[99] = 99.90, 100.00, 99.15, 99.28, 210.0
    return o, h, l, c, v, ema20, ema50


def test_hardened_liquidity_sweep_still_accepts_clean_mirrored_setups():
    long_args = _synthetic_sweep_arrays('long')
    short_args = _synthetic_sweep_arrays('short')
    long = sweep._detect('long', o=long_args[0], h=long_args[1], l=long_args[2], c=long_args[3], v=long_args[4], ema20=long_args[5], ema50=long_args[6], atr_value=1.0)
    short = sweep._detect('short', o=short_args[0], h=short_args[1], l=short_args[2], c=short_args[3], v=short_args[4], ema20=short_args[5], ema50=short_args[6], atr_value=1.0)
    assert long is not None and long['score'] >= sweep.MIN_SCORE
    assert short is not None and short['score'] >= sweep.MIN_SCORE
    assert long['trigger_extension_atr'] <= sweep.TRIGGER_EXTENSION_MAX_ATR
    assert short['trigger_extension_atr'] <= sweep.TRIGGER_EXTENSION_MAX_ATR
