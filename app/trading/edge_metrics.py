"""Per-signal, notional-normalized expected edge at TP/SL (no prediction).

Uses the final executable entry and conservative paper fee/slippage assumptions.
No additional position sizing or risk percentage is introduced.
"""
from __future__ import annotations

import math

from app.models.enums import Direction


def edge_estimate(*, entry: float, stop: float, target: float,
                  direction: Direction, taker_fee_rate: float,
                  exit_slippage_bps: float = 0.0) -> dict[str, float]:
    if not all(math.isfinite(float(x)) for x in
               (entry, stop, target, taker_fee_rate, exit_slippage_bps)):
        raise ValueError("non_finite_edge_input")
    if entry <= 0 or stop <= 0 or target <= 0 or taker_fee_rate < 0 or exit_slippage_bps < 0:
        raise ValueError("invalid_edge_input")
    if direction == Direction.LONG:
        gross_tp = (target - entry) / entry
        gross_sl = (entry - stop) / entry
    elif direction == Direction.SHORT:
        gross_tp = (entry - target) / entry
        gross_sl = (stop - entry) / entry
    else:
        raise ValueError("unsupported_direction")
    if gross_tp <= 0 or gross_sl <= 0:
        raise ValueError("invalid_trade_geometry")
    cost_fraction = taker_fee_rate * 2 + exit_slippage_bps / 10_000
    profit = gross_tp - cost_fraction
    loss = gross_sl + cost_fraction
    return {
        "gross_tp_bps": round(gross_tp * 10_000, 4),
        "gross_sl_bps": round(gross_sl * 10_000, 4),
        "estimated_round_trip_cost_bps": round(cost_fraction * 10_000, 4),
        "estimated_net_tp_bps": round(profit * 10_000, 4),
        "estimated_net_sl_bps": round(-loss * 10_000, 4),
        "estimated_breakeven_win_rate": round(loss / (profit + loss), 6) if profit > 0 else 1.0,
    }
