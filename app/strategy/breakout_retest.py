from __future__ import annotations

from app.models.enums import Direction, Strategy
from app.models.trading import TradeIntent
from app.strategy.source_math import adx, atr, candle_quality, clamp, ema, extract, pct_change

EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200
ADX_PERIOD = 14
EMA_SLOPE_LOOKBACK = 6
MIN_CANDLES_REQUIRED = 260
MIN_NONZERO_VOLUME_RATIO = 0.92

H1_ADX_MIN = 12.0
M15_ADX_MIN = 11.0
M5_ADX_MIN = 9.5
ATR_PCT_MIN = 0.00075
ATR_PCT_MAX = 0.0180
TREND_STACK_MIN_PCT = 0.00030
RESET_LOOKBACK_BARS = 5
RESET_TOUCH_TOL_ATR = 0.38
RESET_BREAK_TOL_ATR = 0.48
TRIGGER_MAX_EMA20_EXTENSION_ATR = 0.95
CONTINUATION_CONFIRM_TOL_ATR = 0.10
MTF_SL_BUFFER_ATR = 0.10
MIN_RR_TO_SIGNAL = 0.95
MIN_SCORE_TO_SIGNAL = 69.0
MAX_SCORE = 100.0
STRENGTH_MIN = 0.20
STRENGTH_MAX = 0.97


def _tf_values(candles):
    o, h, l, c, v = extract(candles)
    return {
        "o": o,
        "h": h,
        "l": l,
        "c": c,
        "v": v,
        "ema20": ema(c, EMA_FAST),
        "ema50": ema(c, EMA_MID),
        "ema200": ema(c, EMA_SLOW),
        "adx": adx(h, l, c, ADX_PERIOD),
        "atr": atr(h, l, c, ADX_PERIOD),
    }


def _bias(tf: dict, *, adx_min: float) -> tuple[str, dict]:
    c = tf["c"]
    ema20 = tf["ema20"]
    ema50 = tf["ema50"]
    ema200 = tf["ema200"]
    if not c or not ema20 or not ema50 or not ema200:
        return "none", {"reason": "NO_TF_DATA"}
    close = float(c[-1])
    adx_value = float(tf["adx"])
    atr_pct = float(tf["atr"]) / max(close, 1e-12)
    slope_idx20 = max(0, len(ema20) - 1 - EMA_SLOPE_LOOKBACK)
    slope_idx50 = max(0, len(ema50) - 1 - EMA_SLOPE_LOOKBACK)
    slope20 = pct_change(float(ema20[-1]), float(ema20[slope_idx20] or ema20[-1]))
    slope50 = pct_change(float(ema50[-1]), float(ema50[slope_idx50] or ema50[-1]))
    stack_spread = abs(float(ema20[-1]) - float(ema50[-1])) / max(close, 1e-12)
    long_bias = (
        close > ema20[-1] > ema50[-1] > ema200[-1]
        and slope20 > 0.0
        and slope50 >= -0.00010
        and adx_value >= adx_min
        and stack_spread >= TREND_STACK_MIN_PCT
        and close >= ema20[-1]
    )
    short_bias = (
        close < ema20[-1] < ema50[-1] < ema200[-1]
        and slope20 < 0.0
        and slope50 <= 0.00010
        and adx_value >= adx_min
        and stack_spread >= TREND_STACK_MIN_PCT
        and close <= ema20[-1]
    )
    direction = "long" if long_bias else "short" if short_bias else "none"
    return direction, {
        "adx": round(adx_value, 2),
        "atr_pct": round(atr_pct, 6),
        "slope20": round(slope20, 6),
        "slope50": round(slope50, 6),
        "stack_spread": round(stack_spread, 6),
        "close": round(close, 8),
        "ema20": round(float(ema20[-1]), 8),
        "ema50": round(float(ema50[-1]), 8),
        "ema200": round(float(ema200[-1]), 8),
    }


def _trigger(direction: str, tf: dict) -> tuple[bool, str, dict]:
    o, h, l, c = tf["o"], tf["h"], tf["l"], tf["c"]
    ema20, ema50 = tf["ema20"], tf["ema50"]
    atr_value = float(tf["atr"])
    if len(c) < max(EMA_SLOW + 5, 80):
        return False, "NOT_ENOUGH_BARS", {}
    i = len(c) - 1
    recent_idx = list(range(max(0, i - RESET_LOOKBACK_BARS), i))
    if not recent_idx:
        return False, "NO_RESET_WINDOW", {}
    reset_low = min(l[j] for j in recent_idx)
    reset_high = max(h[j] for j in recent_idx)
    min_ema20 = min(float(ema20[j]) for j in recent_idx)
    max_ema20 = max(float(ema20[j]) for j in recent_idx)
    min_ema50 = min(float(ema50[j]) for j in recent_idx)
    max_ema50 = max(float(ema50[j]) for j in recent_idx)
    extension_atr = abs(float(c[i]) - float(ema20[i])) / max(atr_value, 1e-12)
    prev_high = float(h[i - 1]) if i >= 1 else float(h[i])
    prev_low = float(l[i - 1]) if i >= 1 else float(l[i])
    confirm_tol = atr_value * CONTINUATION_CONFIRM_TOL_ATR

    if direction == "long":
        diag = {
            "reset_low": reset_low,
            "ema20_ref": max_ema20,
            "ema50_ref": min_ema50,
            "extension_atr": extension_atr,
            "prev_high": prev_high,
        }
        if reset_low > max_ema20 + atr_value * RESET_TOUCH_TOL_ATR:
            return False, "NO_5M_RESET_TOUCH", diag
        if reset_low < min_ema50 - atr_value * RESET_BREAK_TOL_ATR:
            return False, "RESET_TOO_DEEP", diag
        reclaim_ok = (
            float(c[i]) > float(ema20[i])
            and float(c[i]) > float(o[i])
            and (float(c[i]) >= prev_high - confirm_tol or float(h[i]) >= prev_high)
        )
        if not reclaim_ok:
            return False, "NO_5M_CONTINUATION_CONFIRM", diag
    else:
        diag = {
            "reset_high": reset_high,
            "ema20_ref": min_ema20,
            "ema50_ref": max_ema50,
            "extension_atr": extension_atr,
            "prev_low": prev_low,
        }
        if reset_high < min_ema20 - atr_value * RESET_TOUCH_TOL_ATR:
            return False, "NO_5M_RESET_TOUCH", diag
        if reset_high > max_ema50 + atr_value * RESET_BREAK_TOL_ATR:
            return False, "RESET_TOO_DEEP", diag
        reclaim_ok = (
            float(c[i]) < float(ema20[i])
            and float(c[i]) < float(o[i])
            and (float(c[i]) <= prev_low + confirm_tol or float(l[i]) <= prev_low)
        )
        if not reclaim_ok:
            return False, "NO_5M_CONTINUATION_CONFIRM", diag

    if extension_atr > TRIGGER_MAX_EMA20_EXTENSION_ATR:
        return False, "TOO_EXTENDED_AFTER_CONFIRM", diag
    return True, "OK", diag


def _structure_target(direction, close, highs5, lows5, highs15, lows15):
    """Nearest opposing swing, or a projection of the latest consolidation range."""
    recent_high = max(highs5[-(RESET_LOOKBACK_BARS + 1):-1])
    recent_low = min(lows5[-(RESET_LOOKBACK_BARS + 1):-1])
    range_size = recent_high - recent_low
    if direction == Direction.LONG:
        levels = [x for x in (max(highs5[-35:-1]), max(highs15[-25:-1])) if x > close]
        return min(levels) if levels else close + range_size
    levels = [x for x in (min(lows5[-35:-1]), min(lows15[-25:-1])) if x < close]
    return max(levels) if levels else close - range_size


def _break_even(score: float, strength: float, sl_pct: float, tp_pct: float) -> tuple[float, float, str]:
    if score >= 88.0 or strength >= 0.88:
        bucket, be_ratio, be_offset = "strong", 0.56, 0.00070
    elif score >= 79.0 or strength >= 0.76:
        bucket, be_ratio, be_offset = "base", 0.50, 0.00060
    else:
        bucket, be_ratio, be_offset = "weak", 0.45, 0.00055
    activation = clamp(min(tp_pct * be_ratio, sl_pct * 0.95), 0.0028, 0.0049)
    return round(activation, 6), round(be_offset, 6), bucket


class BreakoutRetestStrategy:
    def __init__(self):
        self.last_trace = {}

    def _reject(self, reason, **metrics):
        self.last_trace = {"accepted": False, "reason": reason, **metrics}
        return None

    def evaluate(self, regime, candles, decision_id, symbol, timeframe, current_price=None, snapshot=None):
        self.last_trace = {"accepted": False, "reason": "not_evaluated"}
        if regime.hard_block:
            return self._reject("regime_hard_block")
        if not regime.breakout_allowed:
            return self._reject("regime_breakout_not_allowed")

        timeframes = getattr(snapshot, "timeframes", {}) if snapshot is not None else {}
        c5 = list(timeframes.get("5m", candles) or [])
        c15 = list(timeframes.get("15m", []) or [])
        c1h = list(timeframes.get("1h", []) or [])
        ok5, diag5q = candle_quality(c5, MIN_CANDLES_REQUIRED, MIN_NONZERO_VOLUME_RATIO)
        if not ok5:
            return self._reject("bad_5m_candle_quality", **diag5q)
        if len(c15) < 200 or len(c1h) < 200:
            return self._reject("insufficient_mtf_bars", bars_5m=len(c5), bars_15m=len(c15), bars_1h=len(c1h))

        tf5, tf15, tf1h = _tf_values(c5), _tf_values(c15), _tf_values(c1h)
        close5 = float(tf5["c"][-1])
        atr5 = float(tf5["atr"])
        atr_pct = atr5 / max(close5, 1e-12)
        adx5 = float(tf5["adx"])
        if atr_pct < ATR_PCT_MIN:
            return self._reject("atr_too_low", atr_pct=atr_pct, min=ATR_PCT_MIN)
        if atr_pct > ATR_PCT_MAX:
            return self._reject("atr_too_high", atr_pct=atr_pct, max=ATR_PCT_MAX)
        if adx5 < M5_ADX_MIN:
            return self._reject("adx_5m_below_threshold", adx_5m=adx5, min=M5_ADX_MIN)

        bias1h, diag1h = _bias(tf1h, adx_min=H1_ADX_MIN)
        bias15, diag15 = _bias(tf15, adx_min=M15_ADX_MIN)
        if bias1h == "none":
            return self._reject("bias_1h_missing", details=diag1h)
        if bias15 == "none":
            return self._reject("bias_15m_missing", details=diag15)
        if bias1h != bias15:
            return self._reject("mtf_bias_mismatch", bias_1h=bias1h, bias_15m=bias15)

        ok_trigger, trigger_reason, trigger_diag = _trigger(bias1h, tf5)
        if not ok_trigger:
            return self._reject(trigger_reason.lower(), **trigger_diag)

        if bias1h == "long":
            reset_extreme = min(tf5["l"][max(0, len(tf5["l"]) - 1 - RESET_LOOKBACK_BARS): len(tf5["l"]) - 1])
            structural_pct = max(0.0, (close5 - reset_extreme) / max(close5, 1e-12))
            direction = Direction.LONG
        else:
            reset_extreme = max(tf5["h"][max(0, len(tf5["h"]) - 1 - RESET_LOOKBACK_BARS): len(tf5["h"]) - 1])
            structural_pct = max(0.0, (reset_extreme - close5) / max(close5, 1e-12))
            direction = Direction.SHORT

        stop = (reset_extreme - atr5 * MTF_SL_BUFFER_ATR if direction == Direction.LONG
                else reset_extreme + atr5 * MTF_SL_BUFFER_ATR)
        sl_pct = abs(close5 - stop) / close5
        if stop <= 0 or sl_pct <= 0:
            return self._reject('invalid_structural_stop')
        extension_atr = float(trigger_diag.get("extension_atr", 0.0) or 0.0)
        h1_strength = clamp((float(diag1h.get("adx", 0.0)) - H1_ADX_MIN) / 15.0, 0.0, 1.0)
        m15_strength = clamp((float(diag15.get("adx", 0.0)) - M15_ADX_MIN) / 14.0, 0.0, 1.0)
        m5_strength = clamp((adx5 - M5_ADX_MIN) / 13.0, 0.0, 1.0)
        trend_alignment_quality = clamp(
            (float(diag1h.get("stack_spread", 0.0)) + float(diag15.get("stack_spread", 0.0)))
            / max(TREND_STACK_MIN_PCT * 6.0, 1e-12),
            0.0,
            1.0,
        )
        reset_quality = clamp(1.0 - clamp(extension_atr / max(TRIGGER_MAX_EMA20_EXTENSION_ATR, 1e-12), 0.0, 1.0), 0.0, 1.0)
        quality = clamp(
            0.30 * h1_strength
            + 0.25 * m15_strength
            + 0.20 * m5_strength
            + 0.15 * trend_alignment_quality
            + 0.10 * reset_quality,
            0.0,
            1.0,
        )
        score = round(min(MAX_SCORE, 69.0 + 28.0 * quality), 2)
        if score < MIN_SCORE_TO_SIGNAL:
            return self._reject("score_too_low", score=score, min=MIN_SCORE_TO_SIGNAL)

        target = _structure_target(direction, close5, tf5['h'], tf5['l'], tf15['h'], tf15['l'])
        tp_pct = abs(target - close5) / close5
        execution_rr = tp_pct / sl_pct
        if execution_rr < MIN_RR_TO_SIGNAL:
            return self._reject("rr_too_low", execution_rr=execution_rr, min=MIN_RR_TO_SIGNAL)
        if target <= 0:
            return self._reject('invalid_structural_target')
        strength = clamp(score / 100.0, STRENGTH_MIN, STRENGTH_MAX)
        be_activation, be_offset, bucket = _break_even(score, strength, sl_pct, tp_pct)

        self.last_trace = {
            "accepted": True,
            "reason": "setup_valid",
            "score": score,
            "direction": direction.value,
            "atr_pct": atr_pct,
            "adx_5m": adx5,
            "adx_15m": diag15["adx"],
            "adx_1h": diag1h["adx"],
            "sl_pct": sl_pct,
            "tp_pct": tp_pct,
            "entry": close5,
            "stop": stop,
            "target": target,
            "execution_rr": execution_rr,
            "structural_stop_pct": structural_pct,
        }
        return TradeIntent(
            decision_id,
            symbol,
            Strategy.BREAKOUT_RETEST,
            direction,
            close5,
            stop,
            target,
            score,
            regime.risk_multiplier,
            timeframe,
            ("mtf_1h_15m_alignment", "5m_reset_retest", "continuation_confirmed"),
            {
                "strategy_model": "mtf_simple_continuation_5m_v2",
                "score": score,
                "strength": strength,
                "atr_pct": atr_pct,
                "adx5": adx5,
                "adx15": diag15["adx"],
                "adx1h": diag1h["adx"],
                "sl_pct": sl_pct,
                "tp_pct": tp_pct,
                "execution_rr": execution_rr,
                "structural_stop_pct": structural_pct,
                "partial_tp_enabled": False,
                "tp2_price": target,
                "break_even_activation_pct": be_activation,
                "break_even_offset_pct": be_offset,
                "management_bucket": bucket,
            },
        )
