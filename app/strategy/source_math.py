from __future__ import annotations

from typing import Iterable

from app.models.market import Candle


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def extract(candles: Iterable[Candle]) -> tuple[list[float], list[float], list[float], list[float], list[float]]:
    o: list[float] = []
    h: list[float] = []
    l: list[float] = []
    c: list[float] = []
    v: list[float] = []
    for x in candles:
        o.append(float(x.open))
        h.append(float(x.high))
        l.append(float(x.low))
        c.append(float(x.close))
        v.append(float(x.volume))
    return o, h, l, c, v


def ema(series: list[float], period: int) -> list[float]:
    if not series:
        return []
    out = [float(series[0])]
    k = 2.0 / (float(period) + 1.0)
    for value in series[1:]:
        out.append((float(value) * k) + (out[-1] * (1.0 - k)))
    return out


def rma(series: list[float], period: int) -> list[float]:
    if not series:
        return []
    period = max(1, int(period))
    if len(series) < period:
        avg = sum(float(x) for x in series) / len(series)
        return [avg for _ in series]
    out = [0.0] * len(series)
    first = sum(float(x) for x in series[:period]) / period
    out[period - 1] = first
    for i in range(period, len(series)):
        out[i] = ((out[i - 1] * (period - 1)) + float(series[i])) / period
    for i in range(period - 1):
        out[i] = out[period - 1]
    return out


def atr(h: list[float], l: list[float], c: list[float], period: int = 14) -> float:
    if len(h) < 2:
        return 0.0
    tr = [0.0]
    for i in range(1, len(c)):
        tr.append(
            max(
                float(h[i]) - float(l[i]),
                abs(float(h[i]) - float(c[i - 1])),
                abs(float(l[i]) - float(c[i - 1])),
            )
        )
    values = rma(tr, period)
    return float(values[-1] if values else 0.0)


def adx(h: list[float], l: list[float], c: list[float], period: int = 14) -> float:
    if len(h) < period + 2 or len(l) < period + 2 or len(c) < period + 2:
        return 0.0
    plus_dm = [0.0]
    minus_dm = [0.0]
    tr = [0.0]
    for i in range(1, len(c)):
        up = float(h[i]) - float(h[i - 1])
        down = float(l[i - 1]) - float(l[i])
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        tr.append(
            max(
                float(h[i]) - float(l[i]),
                abs(float(h[i]) - float(c[i - 1])),
                abs(float(l[i]) - float(c[i - 1])),
            )
        )
    atr_series = rma(tr, period)
    plus = [100.0 * (p / a) if a else 0.0 for p, a in zip(rma(plus_dm, period), atr_series)]
    minus = [100.0 * (m / a) if a else 0.0 for m, a in zip(rma(minus_dm, period), atr_series)]
    dx = [100.0 * abs(p - m) / (p + m) if (p + m) else 0.0 for p, m in zip(plus, minus)]
    values = rma(dx, period)
    return float(values[-1] if values else 0.0)


def pct_change(now: float, prev: float) -> float:
    return 0.0 if prev == 0 else (float(now) - float(prev)) / float(prev)


def median(values: list[float]) -> float:
    vals = sorted(float(x) for x in values if x is not None)
    if not vals:
        return 0.0
    mid = len(vals) // 2
    if len(vals) % 2:
        return float(vals[mid])
    return float((vals[mid - 1] + vals[mid]) / 2.0)


def relative_volume(volumes: list[float], idx: int, lookback: int = 20) -> float:
    if idx < 0 or idx >= len(volumes):
        return 1.0
    start = max(0, idx - max(lookback, 8))
    window = [max(float(x), 0.0) for x in volumes[start:idx]]
    nonzero = [x for x in window if x > 0.0]
    baseline = median(nonzero or window)
    if baseline <= 0.0:
        return 1.0
    return max(0.0, float(volumes[idx]) / baseline)


def candle_quality(candles: list[Candle], minimum: int = 260, nonzero_ratio_min: float = 0.92) -> tuple[bool, dict]:
    if len(candles) < minimum:
        return False, {"bars": len(candles), "min_bars": minimum}
    recent = candles[-minimum:]
    valid = [x for x in recent if float(x.close) > 0.0 and float(x.high) >= float(x.low)]
    if len(valid) < minimum:
        return False, {"valid_bars": len(valid), "min_bars": minimum}
    nonzero_ratio = sum(1 for x in recent if float(x.volume) > 0.0) / max(len(recent), 1)
    if nonzero_ratio < nonzero_ratio_min:
        return False, {"nonzero_vol_ratio": round(nonzero_ratio, 4), "min_ratio": nonzero_ratio_min}
    return True, {"bars": len(candles), "nonzero_vol_ratio": round(nonzero_ratio, 4)}
