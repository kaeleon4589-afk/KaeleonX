import pytest

from app.strategy import breakout_retest as breakout


def _tf_series(*, direction: str, move_pct: float, range_pos: float = 0.98):
    # Build deterministic higher-timeframe arrays around 100 so the exhaustion
    # guard can be tested without depending on exchange data.
    n = 80
    start = 100.0
    sign = 1.0 if direction == 'long' else -1.0
    end = start * (1.0 + sign * move_pct)
    closes = [start + (end - start) * i / (n - 1) for i in range(n)]
    span = max(abs(end - start), 4.0)
    if direction == 'long':
        low24 = end - span
        high24 = end + span * (1.0 - range_pos) / max(range_pos, 1e-9)
    else:
        high24 = end + span
        low24 = end - span * range_pos / max(1.0 - range_pos, 1e-9)
    highs = [x + 0.25 for x in closes]
    lows = [x - 0.25 for x in closes]
    # Force the last 24h range edge to be deterministic.
    for i in range(n - 24, n):
        frac = (i - (n - 24)) / 23
        if direction == 'long':
            lows[i] = low24 + span * frac * 0.25
            highs[i] = max(closes[i] + 0.1, high24 - span * (1 - frac) * 0.1)
        else:
            highs[i] = high24 - span * frac * 0.25
            lows[i] = min(closes[i] - 0.1, low24 + span * (1 - frac) * 0.1)
    ema20 = [x - sign * 2.0 for x in closes]
    return {
        'c': closes,
        'h': highs,
        'l': lows,
        'ema20': ema20,
        'atr': 0.9,
    }


@pytest.mark.parametrize('direction', ['long', 'short'])
def test_extreme_24h_move_at_range_edge_is_rejected(direction):
    tf1h = _tf_series(direction=direction, move_pct=0.40, range_pos=0.98 if direction == 'long' else 0.02)
    tf15 = _tf_series(direction=direction, move_pct=0.08, range_pos=0.95 if direction == 'long' else 0.05)
    exhausted, diag = breakout._htf_exhaustion(direction, tf1h, tf15)
    assert exhausted is True
    assert abs(diag['htf_24h_move_pct']) >= breakout.HTF_EXTREME_MOVE_PCT


def test_moderate_trend_is_not_rejected_only_for_being_near_range_edge():
    tf1h = _tf_series(direction='long', move_pct=0.03, range_pos=0.97)
    tf15 = _tf_series(direction='long', move_pct=0.02, range_pos=0.97)
    exhausted, diag = breakout._htf_exhaustion('long', tf1h, tf15)
    assert exhausted is False
    assert diag['htf_24h_move_pct'] < breakout.HTF_EXTREME_MOVE_PCT


def test_breakout_strategy_requires_stop_outside_one_normal_5m_atr():
    assert breakout.MIN_SIGNAL_STOP_ATR_5M >= 0.95
    assert breakout.RETEST_MIN_PULLBACK_ATR >= 0.18
