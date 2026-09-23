from __future__ import annotations

import math
import os
from typing import Any

from app.strategy.source_math import adx as source_adx
from app.strategy.source_math import atr as source_atr
from app.strategy.source_math import clamp, ema, extract

TREND = "TREND_CONTINUATION"
VOLATILE = "VOLATILE_SWEEP"
RANGE = "RANGE"
UNKNOWN = "UNKNOWN"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _true_ranges(h: list[float], l: list[float], c: list[float]) -> list[float]:
    if not h or len(h) != len(l) or len(h) != len(c):
        return []
    out = [max(float(h[0]) - float(l[0]), 0.0)]
    for i in range(1, len(c)):
        out.append(
            max(
                float(h[i]) - float(l[i]),
                abs(float(h[i]) - float(c[i - 1])),
                abs(float(l[i]) - float(c[i - 1])),
            )
        )
    return out


def _choppiness(h: list[float], l: list[float], c: list[float], period: int = 14) -> float:
    if len(c) < max(period + 1, 3):
        return 50.0
    hh = max(float(x) for x in h[-period:])
    ll = min(float(x) for x in l[-period:])
    price_range = max(hh - ll, 1e-12)
    tr_sum = sum(_true_ranges(h[-period:], l[-period:], c[-period:]))
    if tr_sum <= 0.0:
        return 50.0
    return clamp(100.0 * math.log10(tr_sum / price_range) / math.log10(float(period)), 0.0, 100.0)


def _efficiency(c: list[float], lookback: int = 20) -> float:
    if len(c) < max(lookback + 1, 3):
        return 0.0
    window = c[-(lookback + 1):]
    net_move = abs(float(window[-1]) - float(window[0]))
    path = sum(abs(float(window[i]) - float(window[i - 1])) for i in range(1, len(window)))
    if path <= 0.0:
        return 0.0
    return clamp(net_move / path, 0.0, 1.0)


def _realized_vol(c: list[float], lookback: int = 20) -> float:
    if len(c) < max(lookback + 1, 4):
        return 0.0
    values = c[-(lookback + 1):]
    rets = []
    for i in range(1, len(values)):
        prev = float(values[i - 1])
        now = float(values[i])
        if prev > 0.0 and now > 0.0:
            rets.append(math.log(now / prev))
    if not rets:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / len(rets)
    return max(0.0, math.sqrt(var))


def _wick_instability(o: list[float], h: list[float], l: list[float], c: list[float], lookback: int = 8) -> float:
    if len(c) < max(lookback, 3):
        return 0.0
    values = []
    for oo, hh, ll, cc in zip(o[-lookback:], h[-lookback:], l[-lookback:], c[-lookback:]):
        rng = max(float(hh) - float(ll), 1e-12)
        body = abs(float(cc) - float(oo))
        values.append(clamp((rng - body) / rng, 0.0, 1.0))
    return sum(values) / len(values) if values else 0.0


def _body_quality(o: list[float], h: list[float], l: list[float], c: list[float], lookback: int = 8) -> float:
    if len(c) < max(lookback, 3):
        return 0.0
    values = []
    for oo, hh, ll, cc in zip(o[-lookback:], h[-lookback:], l[-lookback:], c[-lookback:]):
        rng = max(float(hh) - float(ll), 1e-12)
        body = abs(float(cc) - float(oo))
        values.append(clamp(body / rng, 0.0, 1.0))
    return sum(values) / len(values) if values else 0.0


def _breakout_failure_ratio(h: list[float], l: list[float], c: list[float], lookback: int = 20, sample_size: int = 12) -> float:
    total = 0
    failures = 0
    n = len(c)
    if n < max(lookback + 3, sample_size + 3):
        return 0.0
    start = max(lookback, n - sample_size - 1)
    for i in range(start, n - 1):
        prior_high = max(float(x) for x in h[i - lookback:i])
        prior_low = min(float(x) for x in l[i - lookback:i])
        close_now = float(c[i])
        close_next = float(c[i + 1])
        if close_now > prior_high:
            total += 1
            if close_next < prior_high:
                failures += 1
        elif close_now < prior_low:
            total += 1
            if close_next > prior_low:
                failures += 1
    return clamp(failures / total, 0.0, 1.0) if total else 0.0


def _ema_metrics(close: float, closes: list[float]) -> dict[str, Any]:
    e20s = ema(closes, 20)
    e50s = ema(closes, 50)
    e200s = ema(closes, 200)
    e20 = float(e20s[-1] if e20s else 0.0)
    e50 = float(e50s[-1] if e50s else 0.0)
    e200 = float(e200s[-1] if e200s else 0.0)
    slope20 = float(e20 - (e20s[-6] if len(e20s) >= 6 else e20))
    slope50 = float(e50 - (e50s[-6] if len(e50s) >= 6 else e50))

    bullish = close > e20 > e50 > e200 and slope20 > 0.0 and slope50 >= 0.0
    bearish = close < e20 < e50 < e200 and slope20 < 0.0 and slope50 <= 0.0
    if bullish or bearish:
        alignment = 1.0
    else:
        score = 0.0
        if close > e20:
            score += 0.25
        if e20 > e50:
            score += 0.25
        if e50 > e200:
            score += 0.25
        if slope20 > 0.0:
            score += 0.125
        if slope50 > 0.0:
            score += 0.125
        alignment = clamp(score, 0.0, 1.0)

    return {
        "ema20": e20,
        "ema50": e50,
        "ema200": e200,
        "ema20_slope": slope20,
        "ema50_slope": slope50,
        "ema_stack_alignment": alignment,
        "trend_bias": "long" if bullish else "short" if bearish else "neutral",
        "distance_to_ema20": close - e20,
        "distance_to_ema50": close - e50,
        "distance_to_ema200": close - e200,
        "stack_bullish": bullish,
        "stack_bearish": bearish,
    }


def _vwap_distance(candles, close: float, atr_value: float, lookback: int = 48) -> dict[str, float]:
    if not candles:
        return {"rolling_vwap": 0.0, "distance_to_vwap": 0.0, "distance_to_vwap_atr": 0.0}
    window = candles[-lookback:] if len(candles) > lookback else candles
    notional = 0.0
    volume = 0.0
    for item in window:
        price = (float(item.high) + float(item.low) + float(item.close)) / 3.0
        vol = max(float(item.volume), 0.0)
        notional += price * vol
        volume += vol
    vwap = (notional / volume) if volume > 0.0 else close
    distance = close - vwap
    return {
        "rolling_vwap": vwap,
        "distance_to_vwap": distance,
        "distance_to_vwap_atr": abs(distance) / max(atr_value, 1e-12),
    }


def _recent_move(c: list[float], bars: int = 3) -> float:
    if len(c) < max(bars + 1, 2):
        return 0.0
    prev = float(c[-bars - 1])
    now = float(c[-1])
    return 0.0 if prev == 0.0 else (now - prev) / prev


def features(candles, btc_candles=None):
    """Source-compatible regime feature extraction on KAELEON Candle objects."""
    candles = list(candles or [])
    btc = list(btc_candles or candles)
    if not candles:
        return {"context_ok": False, "context_status": "NO_CANDLES"}

    o, h, l, c, _ = extract(candles)
    close = float(c[-1]) if c else 0.0
    atr_value = float(source_atr(h, l, c, 14))
    atr_pct = atr_value / close if close > 0 else 0.0
    adx_value = float(source_adx(h, l, c, 14))
    ema_info = _ema_metrics(close, c)
    vwap = _vwap_distance(candles, close, atr_value)

    if btc:
        _, bh, bl, bc, _ = extract(btc)
        btc_close = float(bc[-1]) if bc else 0.0
        btc_atr_value = float(source_atr(bh, bl, bc, 14))
        btc_atr_pct = btc_atr_value / btc_close if btc_close > 0 else 0.0
        btc_move3 = _recent_move(bc, 3)
        btc_move6 = _recent_move(bc, 6)
        btc_realized = _realized_vol(bc, 20)
    else:
        btc_atr_pct = btc_move3 = btc_move6 = btc_realized = 0.0

    # Hyperliquid's source market context requires enough bars for EMA200 (+5).
    # CoinW snapshots are already freshness-validated by the coordinator/request.
    context_ok = len(candles) >= 205 and close > 0.0
    btc_context_ok = len(btc) >= 205 if btc else False
    btc_shock_ratio = abs(btc_move3) / btc_atr_pct if btc_atr_pct > 0.0 else 0.0

    return {
        "context_ok": context_ok,
        "context_status": "OK" if context_ok else "INSUFFICIENT_CANDLES",
        "context_stale": False,
        "close": close,
        "atr": atr_value,
        "atr_pct": atr_pct,
        "adx": adx_value,
        "choppiness": _choppiness(h, l, c, 14),
        "efficiency_ratio": _efficiency(c, 20),
        "realized_vol": _realized_vol(c, 20),
        "wick_instability": _wick_instability(o, h, l, c, 8),
        "body_quality": _body_quality(o, h, l, c, 8),
        "breakout_failure_ratio": _breakout_failure_ratio(h, l, c, 20, 12),
        "recent_move_3": _recent_move(c, 3),
        "recent_move_6": _recent_move(c, 6),
        **vwap,
        "btc_context_ok": btc_context_ok,
        "btc_context_status": "OK" if btc_context_ok else "INSUFFICIENT_CANDLES",
        "btc_atr_pct": btc_atr_pct,
        "btc_realized_vol": btc_realized,
        "btc_recent_move_3": btc_move3,
        "btc_recent_move_6": btc_move6,
        "btc_shock_ratio": btc_shock_ratio,
        **ema_info,
    }


def classify_details(f: dict[str, Any]) -> dict[str, Any]:
    """Port of Trading-X-Hiper-Pro regime detector v2_calibrated_router."""
    reasons: list[str] = []
    if not f.get("context_ok"):
        reasons.append(f"symbol_context_not_ok:{f.get('context_status')}")
        return {
            "candidate_regime": UNKNOWN,
            "confidence": 0.0,
            "reasons": reasons,
            "scores": {TREND: 0, VOLATILE: 0, RANGE: 0},
            "regime_bias": "neutral",
        }

    adx_value = float(f.get("adx") or 0.0)
    chop = float(f.get("choppiness") or 50.0)
    efficiency = float(f.get("efficiency_ratio") or 0.0)
    wick = float(f.get("wick_instability") or 0.0)
    body = float(f.get("body_quality") or 0.0)
    failure = float(f.get("breakout_failure_ratio") or 0.0)
    atr_pct = float(f.get("atr_pct") or 0.0)
    btc_shock = float(f.get("btc_shock_ratio") or 0.0)
    vwap_atr = float(f.get("distance_to_vwap_atr") or 0.0)
    alignment = float(f.get("ema_stack_alignment") or 0.0)
    trend_bias = str(f.get("trend_bias") or "neutral")
    move3 = abs(float(f.get("recent_move_3") or 0.0))
    btc_move3 = abs(float(f.get("btc_recent_move_3") or 0.0))

    trend_adx_min = _env_float("REGIME_TREND_ADX_MIN", 15.0)
    trend_chop_max = _env_float("REGIME_TREND_CHOP_MAX", 57.0)
    trend_eff_min = _env_float("REGIME_TREND_EFFICIENCY_MIN", 0.23)
    trend_align_min = _env_float("REGIME_TREND_EMA_ALIGN_MIN", 0.54)
    trend_fail_max = _env_float("REGIME_TREND_BREAKOUT_FAIL_MAX", 0.26)
    volatile_shock_min = _env_float("REGIME_VOLATILE_BTC_SHOCK_MIN", 1.20)
    volatile_wick_min = _env_float("REGIME_VOLATILE_WICK_MIN", 0.48)
    volatile_fail_min = _env_float("REGIME_VOLATILE_BREAKOUT_FAIL_MIN", 0.18)
    volatile_atr_min = _env_float("REGIME_VOLATILE_ATR_PCT_MIN", 0.0060)
    range_adx_max = _env_float("REGIME_RANGE_ADX_MAX", 22.0)
    range_chop_min = _env_float("REGIME_RANGE_CHOP_MIN", 51.0)
    range_eff_max = _env_float("REGIME_RANGE_EFFICIENCY_MAX", 0.38)
    range_vwap_max = _env_float("REGIME_RANGE_VWAP_DIST_ATR_MAX", 1.25)
    range_align_max = _env_float("REGIME_RANGE_EMA_ALIGN_MAX", 0.68)

    volatile_score = sum((
        btc_shock >= volatile_shock_min,
        wick >= volatile_wick_min,
        failure >= volatile_fail_min,
        atr_pct >= volatile_atr_min,
        body <= 0.45 and efficiency <= 0.48,
        move3 >= max(atr_pct * 0.55, 0.0035) or btc_move3 >= 0.0035,
    ))
    trend_score = sum((
        adx_value >= trend_adx_min,
        chop <= trend_chop_max,
        efficiency >= trend_eff_min,
        alignment >= trend_align_min,
        failure <= trend_fail_max,
        body >= 0.35,
    ))
    range_score = sum((
        adx_value <= range_adx_max,
        chop >= range_chop_min,
        efficiency <= range_eff_max,
        vwap_atr <= range_vwap_max,
        alignment <= range_align_max,
        failure >= 0.07 or wick >= 0.42,
    ))

    scores = {TREND: int(trend_score), VOLATILE: int(volatile_score), RANGE: int(range_score)}
    best_regime = max(scores, key=scores.get)
    best_score = int(scores[best_regime])
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    second_score = int(ordered[1][1]) if len(ordered) > 1 else 0

    trend_ready = (
        trend_score >= 4
        or (
            trend_score >= 3
            and adx_value >= trend_adx_min * 0.95
            and efficiency >= trend_eff_min * 0.90
            and alignment >= max(0.50, trend_align_min * 0.90)
        )
    )
    volatile_ready = (
        volatile_score >= 4
        or (
            volatile_score >= 3
            and (
                btc_shock >= volatile_shock_min * 0.92
                or wick >= max(volatile_wick_min * 0.92, 0.46)
                or failure >= volatile_fail_min * 0.95
                or atr_pct >= volatile_atr_min * 1.05
            )
        )
        or (
            volatile_score >= 2
            and wick >= max(volatile_wick_min + 0.02, 0.50)
            and failure >= volatile_fail_min
            and atr_pct >= volatile_atr_min
        )
    )
    range_ready = (
        range_score >= 4
        or (
            range_score >= 3
            and adx_value <= range_adx_max * 1.10
            and chop >= range_chop_min * 0.92
            and efficiency <= max(range_eff_max * 1.10, 0.42)
            and vwap_atr <= range_vwap_max * 1.15
        )
    )

    volatile_decisive = (
        btc_shock >= volatile_shock_min * 1.02
        or wick >= max(volatile_wick_min + 0.03, 0.56)
        or (failure >= volatile_fail_min + 0.06 and atr_pct >= volatile_atr_min * 1.05)
    )
    trend_decisive = alignment >= max(trend_align_min + 0.06, 0.64) or failure <= min(trend_fail_max * 0.80, 0.14)
    range_decisive = vwap_atr <= min(range_vwap_max * 0.82, 0.95) or chop >= max(range_chop_min + 3.0, 56.0)

    if volatile_ready and volatile_score >= max(trend_score, range_score) and (volatile_score - second_score >= 1 or volatile_decisive):
        candidate = VOLATILE
        confidence = min(0.99, 0.42 + 0.08 * volatile_score + 0.03 * max(0, volatile_score - second_score))
        reasons.extend([
            f"volatile_score={volatile_score}", f"btc_shock_ratio={btc_shock:.2f}",
            f"wick_instability={wick:.2f}", f"breakout_failure_ratio={failure:.2f}",
            f"atr_pct={atr_pct:.4f}",
        ])
    elif trend_ready and trend_score >= max(range_score, volatile_score) and (trend_score - second_score >= 2 or trend_decisive):
        candidate = TREND
        confidence = min(0.99, 0.40 + 0.07 * trend_score + 0.03 * max(0, trend_score - second_score))
        reasons.extend([
            f"trend_score={trend_score}", f"adx={adx_value:.2f}", f"choppiness={chop:.2f}",
            f"efficiency_ratio={efficiency:.2f}", f"ema_stack_alignment={alignment:.2f}",
        ])
    elif range_ready and range_score >= max(trend_score, volatile_score) and (range_score - second_score >= 2 or range_decisive):
        candidate = RANGE
        confidence = min(0.99, 0.40 + 0.07 * range_score + 0.03 * max(0, range_score - second_score))
        reasons.extend([
            f"range_score={range_score}", f"adx={adx_value:.2f}", f"choppiness={chop:.2f}",
            f"efficiency_ratio={efficiency:.2f}", f"distance_to_vwap_atr={vwap_atr:.2f}",
        ])
    elif best_score >= 3 and (best_score - second_score) >= 1:
        candidate = best_regime
        confidence = min(0.78, 0.31 + 0.07 * best_score + 0.02 * max(0, best_score - second_score))
        reasons.extend([
            f"soft_classification={best_regime.lower()}",
            f"scores trend={trend_score} volatile={volatile_score} range={range_score}",
            f"adx={adx_value:.2f}", f"choppiness={chop:.2f}",
            f"efficiency_ratio={efficiency:.2f}", f"distance_to_vwap_atr={vwap_atr:.2f}",
        ])
    else:
        candidate = UNKNOWN
        confidence = 0.20
        reasons.extend([
            f"mixed_scores trend={trend_score} volatile={volatile_score} range={range_score}",
            f"adx={adx_value:.2f}", f"choppiness={chop:.2f}",
            f"efficiency_ratio={efficiency:.2f}", f"ema_stack_alignment={alignment:.2f}",
            f"distance_to_vwap_atr={vwap_atr:.2f}",
        ])

    return {
        "candidate_regime": candidate,
        "confidence": confidence,
        "reasons": reasons,
        "scores": scores,
        "regime_bias": trend_bias,
    }


def classify(f):
    details = classify_details(f)
    return details["candidate_regime"], details["confidence"], details["scores"]
