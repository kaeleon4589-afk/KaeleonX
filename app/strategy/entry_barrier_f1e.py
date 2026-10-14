"""Phase 1E: narrow support/resistance runway validation for BREAKOUT_RETEST.

This is NOT the complete Phase 1F structural engine. It only rejects
continuations whose pre-existing, confirmed opposing pivot band blocks
profitable travel before the unchanged strategy target. Does not modify
margin, leverage, TP/SL, sweep signals, or create cooldowns.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time

from app.models.enums import Direction, Strategy

SPAN_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000}


@dataclass(frozen=True)
class Zone:
    price: float
    half_width: float
    kind: str
    timeframe: str
    touches: int


def _closed(rows, timeframe: str, now_ms: int) -> list:
    span = SPAN_MS[timeframe]
    out = []
    for c in rows or ():
        try:
            ts = int(c.timestamp)
            prices = (float(c.open), float(c.high), float(c.low), float(c.close))
            if ts > 0 and ts + span <= now_ms and all(math.isfinite(v) and v > 0 for v in prices):
                out.append(c)
        except (TypeError, ValueError, AttributeError):
            continue
    return sorted(out, key=lambda x: int(x.timestamp))


def _atr5(bars: list) -> float:
    if len(bars) < 15:
        return 0.0
    recent = bars[-15:]
    previous = float(bars[-16].close) if len(bars) >= 16 else float(bars[-15].close)
    ranges = []
    for c in recent:
        high, low = float(c.high), float(c.low)
        ranges.append(max(high-low, abs(high-previous), abs(low-previous)))
        previous = float(c.close)
    return sum(ranges)/len(ranges)


def build_context(snapshot, *, now_ms: int | None = None) -> dict:
    """Closed, confirmed pivot zones shared per market (no intra-candle look-ahead)."""
    now_ms = int(time.time()*1000) if now_ms is None else int(now_ms)
    frames = dict(getattr(snapshot, 'timeframes', {}) or {})
    if '5m' not in frames:
        frames['5m'] = list(getattr(snapshot, 'candles', ()) or ())
    closed = {tf: _closed(frames.get(tf), tf, now_ms) for tf in SPAN_MS}
    atr = _atr5(closed['5m'])
    if not atr:
        return {"zones": (), "last_5m_ts": 0, "atr": 0.0, "sources": ()}

    zones: list[Zone] = []
    for tf, bars in closed.items():
        if len(bars) < 9:
            continue
        # Confirmed fractal requires 2 completed candles to the right.
        pivots = {"support": [], "resistance": []}
        for idx in range(max(2, len(bars)-105), len(bars)-2):
            window = bars[idx-2:idx+3]
            low, high = float(bars[idx].low), float(bars[idx].high)
            if low <= min(float(x.low) for x in window) and any(low < float(x.low) for x in window if x is not bars[idx]):
                pivots['support'].append((idx, low))
            if high >= max(float(x.high) for x in window) and any(high > float(x.high) for x in window if x is not bars[idx]):
                pivots['resistance'].append((idx, high))
        width = 0.20 * atr if tf == '5m' else (0.28 * atr if tf == '15m' else 0.35*atr)
        for kind, points in pivots.items():
            clusters: list[list[tuple[int,float]]] = []
            for idx, price in points:
                match = next((g for g in clusters if abs(price-sum(p for _,p in g)/len(g)) <= width), None)
                if match is None:
                    clusters.append([(idx,price)])
                elif not match or idx - match[-1][0] >= 3:
                    match.append((idx,price))
            for group in clusters:
                # Ignore one-off 5m wicks. A single CONFIRMED 15m/1h swing
                # is meaningful enough to be diagnostic, not automatically fatal.
                if tf == '5m' and len(group) < 2:
                    continue
                level = sum(p for _,p in group)/len(group)
                zones.append(Zone(level, width, kind, tf, len(group)))
    return {"zones": tuple(zones), "atr": atr,
            "last_5m_ts": int(closed['5m'][-1].timestamp),
            "sources": tuple(tf for tf, rows in closed.items() if len(rows) >= 9)}


def evaluate_barrier(*, context, direction: Direction, strategy: Strategy,
                     entry: float, stop: float, target: float,
                     fee_rate: float = .0006, slippage_bps: float = 0.0):
    """Check only confirmed obstacle in direction of entry, never infer one.

    Breakeven runway is measured to the near edge of the opposing zone after
    TWO taker fees + simulated exit slippage, against projected SL loss + costs.
    """
    if strategy != Strategy.BREAKOUT_RETEST:
        return None, {"barrier_check": "other_strategy_unchanged"}
    ctx = context or {}
    sources = tuple(ctx.get("sources") or ())
    if not sources or not ctx.get("zones"):
        return None, {"barrier_check": "no_confirmed_obstacle", "sources": sources}
    try:
        entry, stop, target = float(entry), float(stop), float(target)
        fee_rate, slippage_bps = max(0.0, float(fee_rate)), max(0.0, float(slippage_bps))
    except (TypeError, ValueError):
        return None, {"barrier_check": "invalid_prices_use_existing_guards"}
    if not all(math.isfinite(x) for x in (entry, stop, target, fee_rate, slippage_bps)):
        return None, {"barrier_check": "nonfinite_prices_use_existing_guards"}
    if direction == Direction.SHORT and not (target < entry < stop):
        return None, {"barrier_check": "invalid_short_geometry_use_existing_guards"}
    if direction == Direction.LONG and not (stop < entry < target):
        return None, {"barrier_check": "invalid_long_geometry_use_existing_guards"}
    if direction not in (Direction.SHORT, Direction.LONG):
        return None, {"barrier_check": "unsupported_direction"}
    cost = entry * (2.0 * fee_rate + slippage_bps / 10_000.0)
    loss_with_cost = abs(entry - stop) + cost
    obstacles = []
    opposing = 'support' if direction == Direction.SHORT else 'resistance'
    for zone in ctx["zones"]:
        if zone.kind != opposing:
            continue
        upper = zone.price + zone.half_width
        lower = zone.price - zone.half_width
        if direction == Direction.SHORT:
            if not (target < upper and entry > lower):
                continue
            free_distance = max(0.0, entry - upper)
        else:
            if not (entry < upper and target > lower):
                continue
            free_distance = max(0.0, lower - entry)
        obstacles.append((free_distance, zone))
    if not obstacles:
        return None, {"barrier_check": "clear_to_target", "sources": sources}
    free_distance, zone = min(obstacles, key=lambda item: item[0])
    net_free_rr = (free_distance - cost) / max(1e-12, loss_with_cost)
    detail = {"nearest_barrier": round(zone.price, 8),
              "barrier_band_low": round(zone.price-zone.half_width, 8),
              "barrier_band_high": round(zone.price+zone.half_width, 8),
              "barrier_timeframe": zone.timeframe,
              "barrier_touches": zone.touches,
              "barrier_free_net_rr": round(net_free_rr, 4),
              "sources": sources}
    if net_free_rr < 1.0:
        return ('support_too_close' if direction == Direction.SHORT
                else 'resistance_too_close'), detail
    return None, detail
