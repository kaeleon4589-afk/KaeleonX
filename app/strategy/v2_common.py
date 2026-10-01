from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from typing import Any

from app.models.enums import Direction, Strategy
from app.strategy.source_math import adx, atr, ema, extract, median, relative_volume


def env_float(name: str, default: float, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = float(default)
    if minimum is not None:
        value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return value


def env_int(name: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = int(default)
    if minimum is not None:
        value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return value


def clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, float(value)))


def frames(snapshot, fallback=None) -> dict[str, list]:
    tf = dict(getattr(snapshot, "timeframes", {}) or {}) if snapshot is not None else {}
    if "5m" not in tf and fallback is not None:
        tf["5m"] = list(fallback or [])
    return tf


def candle_shape(candle) -> dict[str, float]:
    o = float(candle.open)
    h = float(candle.high)
    l = float(candle.low)
    c = float(candle.close)
    rng = max(h - l, 1e-12)
    body = abs(c - o)
    return {
        "range": rng,
        "body": body,
        "body_ratio": clamp(body / rng, 0.0, 1.0),
        "close_pos": clamp((c - l) / rng, 0.0, 1.0),
        "lower_wick_ratio": clamp((min(o, c) - l) / rng, 0.0, 1.0),
        "upper_wick_ratio": clamp((h - max(o, c)) / rng, 0.0, 1.0),
    }


def efficiency_ratio(closes: list[float], lookback: int = 20) -> float:
    if len(closes) < lookback + 1:
        return 0.0
    window = closes[-(lookback + 1):]
    path = sum(abs(window[i] - window[i - 1]) for i in range(1, len(window)))
    if path <= 0:
        return 0.0
    return clamp(abs(window[-1] - window[0]) / path, 0.0, 1.0)


def choppiness(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    tr = []
    for i in range(len(closes) - period, len(closes)):
        prev = closes[i - 1]
        tr.append(max(highs[i] - lows[i], abs(highs[i] - prev), abs(lows[i] - prev)))
    high = max(highs[-period:])
    low = min(lows[-period:])
    span = max(high - low, 1e-12)
    total = max(sum(tr), 1e-12)
    return clamp(100.0 * math.log10(total / span) / math.log10(float(period)), 0.0, 100.0)


def trend_alignment(candles: list, *, fast: int = 20, mid: int = 50, slow: int = 200) -> tuple[str, float, dict]:
    if len(candles) < slow + 5:
        return "neutral", 0.0, {"reason": "insufficient_bars"}
    _, _, _, closes, _ = extract(candles)
    efast = ema(closes, fast)
    emid = ema(closes, mid)
    eslow = ema(closes, slow)
    close = float(closes[-1])
    f = float(efast[-1])
    m = float(emid[-1])
    s = float(eslow[-1])
    fslope = f - float(efast[-6])
    mslope = m - float(emid[-6])
    bull = sum((close > f, f > m, m > s, fslope > 0, mslope >= 0)) / 5.0
    bear = sum((close < f, f < m, m < s, fslope < 0, mslope <= 0)) / 5.0
    if bull >= 0.60 and bull - bear >= 0.20:
        direction = "long"
        score = bull
    elif bear >= 0.60 and bear - bull >= 0.20:
        direction = "short"
        score = bear
    else:
        direction = "neutral"
        score = max(bull, bear)
    return direction, float(score), {
        "close": close,
        "ema20": f,
        "ema50": m,
        "ema200": s,
        "ema20_slope": fslope,
        "ema50_slope": mslope,
        "bull_alignment": bull,
        "bear_alignment": bear,
    }


def orderbook_imbalance(snapshot, depth: int = 12) -> float | None:
    bids = list(getattr(snapshot, "bids", []) or [])[:depth]
    asks = list(getattr(snapshot, "asks", []) or [])[:depth]
    if len(bids) < 3 or len(asks) < 3:
        return None
    try:
        bid_value = sum(float(p) * float(q) for p, q, *_ in bids)
        ask_value = sum(float(p) * float(q) for p, q, *_ in asks)
    except (TypeError, ValueError):
        return None
    total = bid_value + ask_value
    if total <= 0:
        return None
    return clamp((bid_value - ask_value) / total, -1.0, 1.0)


def executable_price(snapshot, direction: Direction) -> float | None:
    try:
        if direction == Direction.LONG:
            value = float(getattr(snapshot, "ask", 0.0) or 0.0)
        else:
            value = float(getattr(snapshot, "bid", 0.0) or 0.0)
        if value > 0 and math.isfinite(value):
            return value
    except (TypeError, ValueError):
        pass
    try:
        last = getattr(snapshot, "last", None)
        value = float(last.close if hasattr(last, "close") else last)
        return value if value > 0 and math.isfinite(value) else None
    except (TypeError, ValueError, AttributeError):
        return None


def enforce_stop_distance(entry: float, raw_stop: float, atr_value: float, direction: Direction,
                          *, min_atr: float, max_atr: float) -> tuple[float, float] | None:
    if entry <= 0 or atr_value <= 0 or raw_stop <= 0:
        return None
    distance = abs(entry - raw_stop)
    minimum = atr_value * min_atr
    maximum = atr_value * max_atr
    distance = max(distance, minimum)
    if distance > maximum:
        return None
    stop = entry - distance if direction == Direction.LONG else entry + distance
    return float(stop), float(distance)


def target_from_structure(entry: float, stop: float, structural_target: float, direction: Direction,
                          *, target_rr: float, min_rr: float = 1.05, front_run_ratio: float = 0.92) -> tuple[float, float] | None:
    risk = abs(entry - stop)
    if entry <= 0 or stop <= 0 or risk <= 1e-12:
        return None
    if direction == Direction.LONG:
        if structural_target <= entry:
            structural_target = entry + risk * target_rr
        structural = entry + (structural_target - entry) * front_run_ratio
        cap = entry + risk * target_rr
        target = min(structural, cap)
        reward = target - entry
    else:
        if structural_target >= entry or structural_target <= 0:
            structural_target = entry - risk * target_rr
        structural = entry - (entry - structural_target) * front_run_ratio
        cap = entry - risk * target_rr
        target = max(structural, cap)
        reward = entry - target
    rr = reward / risk
    if target <= 0 or rr < min_rr:
        return None
    return float(target), float(rr)


def swing_barrier(candles: list, direction: Direction, entry: float, lookback: int = 80) -> float | None:
    if not candles:
        return None
    window = list(candles[-lookback:])
    values: list[float] = []
    for i in range(2, len(window) - 2):
        if direction == Direction.LONG:
            p = float(window[i].high)
            if p > entry and p >= max(float(window[j].high) for j in range(i - 2, i + 3)):
                values.append(p)
        else:
            p = float(window[i].low)
            if p < entry and p <= min(float(window[j].low) for j in range(i - 2, i + 3)):
                values.append(p)
    if not values:
        return None
    return min(values) if direction == Direction.LONG else max(values)


@dataclass(frozen=True)
class StrategyCandidate:
    strategy: Strategy
    direction: Direction
    stage: str  # READY or WATCH
    quality: float
    signal_entry: float
    trigger_price: float
    invalidation_price: float
    stop_price: float
    target_price: float
    structural_target: float
    entry_zone_low: float
    entry_zone_high: float
    structure_ts: int
    reasons: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def signature(self) -> str:
        level = self.metadata.get("structure_level", self.trigger_price)
        return f"{self.strategy.value}|{self.direction.value}|{self.structure_ts}|{float(level):.12g}"


def market_stats(candles: list) -> dict[str, float]:
    o, h, l, c, v = extract(candles)
    atr_value = float(atr(h, l, c, 14))
    close = float(c[-1]) if c else 0.0
    return {
        "atr": atr_value,
        "atr_pct": atr_value / max(close, 1e-12),
        "adx": float(adx(h, l, c, 14)),
        "efficiency": efficiency_ratio(c, 20),
        "choppiness": choppiness(h, l, c, 14),
        "rvol": float(relative_volume(v, len(v) - 1, 20)) if v else 0.0,
        "median_volume": float(median(v[-20:])) if v else 0.0,
    }
