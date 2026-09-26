from types import SimpleNamespace

import pytest

from app.strategy import breakout_retest as breakout


def _tf(direction: str = 'long'):
    n = 260
    if direction == 'long':
        o = [99.70] * n
        h = [100.00] * n
        l = [99.45] * n
        c = [99.72] * n
        # breakout -> retest -> fresh confirmation
        o[-3], h[-3], l[-3], c[-3] = 99.92, 100.26, 99.90, 100.20
        o[-2], h[-2], l[-2], c[-2] = 100.18, 100.21, 99.96, 100.05
        o[-1], h[-1], l[-1], c[-1] = 100.04, 100.36, 100.02, 100.30
        ema20 = [99.85] * n
        ema50 = [99.55] * n
    else:
        o = [100.30] * n
        h = [100.55] * n
        l = [100.00] * n
        c = [100.28] * n
        o[-3], h[-3], l[-3], c[-3] = 100.08, 100.10, 99.74, 99.80
        o[-2], h[-2], l[-2], c[-2] = 99.82, 100.04, 99.79, 99.95
        o[-1], h[-1], l[-1], c[-1] = 99.96, 99.98, 99.64, 99.70
        ema20 = [100.15] * n
        ema50 = [100.45] * n
    v = [100.0] * n
    v[-3], v[-2], v[-1] = 160.0, 110.0, 150.0
    return {
        'o': o, 'h': h, 'l': l, 'c': c, 'v': v,
        'ema20': ema20, 'ema50': ema50, 'atr': 1.0,
    }


@pytest.mark.parametrize('direction', ['long', 'short'])
def test_true_breakout_retest_requires_fresh_three_phase_sequence(direction):
    ok, reason, diag = breakout._trigger(direction, _tf(direction))
    assert ok is True, (reason, diag)
    assert reason == 'OK'
    assert diag['breakout_age_bars'] == 2
    assert diag['retest_bars'] == 1
    assert diag['retest_touch'] is True
    assert diag['last_retest_near'] is True
    assert diag['structure_extension_atr'] <= breakout.ENTRY_MAX_STRUCTURE_EXTENSION_ATR


@pytest.mark.parametrize('direction', ['long', 'short'])
def test_third_developed_candle_after_retest_is_rejected_as_stale(direction):
    tf = _tf(direction)
    # Insert one developed continuation candle between retest and current trigger.
    for key in ('o', 'h', 'l', 'c', 'v', 'ema20', 'ema50'):
        tf[key].insert(-1, tf[key][-2])
    if direction == 'long':
        tf['o'][-2], tf['h'][-2], tf['l'][-2], tf['c'][-2] = 100.10, 100.56, 100.08, 100.48
        tf['o'][-1], tf['h'][-1], tf['l'][-1], tf['c'][-1] = 100.47, 100.76, 100.44, 100.70
    else:
        tf['o'][-2], tf['h'][-2], tf['l'][-2], tf['c'][-2] = 99.90, 99.92, 99.44, 99.52
        tf['o'][-1], tf['h'][-1], tf['l'][-1], tf['c'][-1] = 99.53, 99.56, 99.24, 99.30
    tf['v'][-2] = 145.0
    tf['v'][-1] = 150.0

    ok, reason, diag = breakout._trigger(direction, tf)
    assert ok is False
    assert reason in {'NO_FRESH_BREAKOUT_RETEST', 'IMPULSE_ALREADY_EXTENDED'}
    assert diag.get('retest_stayed_near') is False or diag.get('structure_extension_atr', 0) > breakout.ENTRY_MAX_STRUCTURE_EXTENSION_ATR


def test_rebound_without_structural_breakout_is_not_a_breakout_retest():
    tf = _tf('long')
    # Remove the real breakout: several green rebound candles remain, but none closes
    # over the 20-bar structural high at 100.00 with the required buffer.
    tf['o'][-3], tf['h'][-3], tf['l'][-3], tf['c'][-3] = 99.55, 99.92, 99.50, 99.88
    tf['o'][-2], tf['h'][-2], tf['l'][-2], tf['c'][-2] = 99.86, 99.98, 99.80, 99.95
    tf['o'][-1], tf['h'][-1], tf['l'][-1], tf['c'][-1] = 99.94, 100.03, 99.90, 99.99

    ok, reason, _ = breakout._trigger('long', tf)
    assert ok is False
    assert reason in {'NO_FRESH_CONFIRMATION', 'NO_FRESH_BREAKOUT_RETEST'}


def test_freshness_constants_prevent_late_impulse_entries():
    assert breakout.RETEST_MAX_BARS_AFTER_BREAKOUT <= 3
    assert breakout.RETEST_MAX_CLOSE_DISTANCE_ATR <= 0.30
    assert breakout.ENTRY_MAX_STRUCTURE_EXTENSION_ATR <= 0.75
    assert breakout.MAX_IMPULSE_CONSUMED_RATIO <= 0.45
