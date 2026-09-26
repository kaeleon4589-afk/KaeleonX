from __future__ import annotations

from app.models.enums import Direction, Strategy
from app.models.trading import TradeIntent
from app.position.protection import (
    break_even_activation_ratio, front_run_target,
    profit_lock_activation_ratio, profit_lock_capture_ratio,
)
from app.strategy.source_math import adx, atr, candle_quality, clamp, ema, extract, pct_change, relative_volume

EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200
ADX_PERIOD = 14
EMA_SLOPE_LOOKBACK = 6
MIN_CANDLES_REQUIRED = 260
MIN_NONZERO_VOLUME_RATIO = 0.92

H1_ADX_MIN = 16.0
M15_ADX_MIN = 14.0
M5_ADX_MIN = 12.0
ATR_PCT_MIN = 0.00075
ATR_PCT_MAX = 0.0180
TREND_STACK_MIN_PCT = 0.00030

# Real BREAKOUT -> RETEST -> FRESH CONFIRMATION model.
STRUCTURE_LOOKBACK_BARS = 20
RETEST_MAX_BARS_AFTER_BREAKOUT = 3
BREAKOUT_CLOSE_BUFFER_ATR = 0.05
BREAKOUT_MAX_EXTENSION_ATR = 0.60
BREAKOUT_MIN_BODY_RATIO = 0.30
BREAKOUT_CLOSE_POS_LONG_MIN = 0.64
BREAKOUT_CLOSE_POS_SHORT_MAX = 0.36
BREAKOUT_MIN_RVOL = 0.95
RETEST_TOUCH_TOL_ATR = 0.18
RETEST_MAX_PENETRATION_ATR = 0.30
RETEST_CLOSE_INVALIDATION_ATR = 0.10
RETEST_MAX_CLOSE_DISTANCE_ATR = 0.28
CONFIRM_BREAK_BUFFER_ATR = 0.02
TRIGGER_MIN_BODY_RATIO = 0.30
TRIGGER_CLOSE_POS_LONG_MIN = 0.66
TRIGGER_CLOSE_POS_SHORT_MAX = 0.34
TRIGGER_MIN_RVOL = 0.90
TRIGGER_MAX_EMA20_EXTENSION_ATR = 2.00  # telemetry guard; structural extension is the anti-late gate
ENTRY_MAX_STRUCTURE_EXTENSION_ATR = 0.75
MAX_IMPULSE_CONSUMED_RATIO = 0.45
MTF_SL_BUFFER_ATR = 0.10
MIN_RR_TO_SIGNAL = 1.05
MIN_SCORE_TO_SIGNAL = 78.0
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


def _candle_shape(o, h, l, c, idx: int) -> tuple[float, float]:
    candle_range = max(float(h[idx]) - float(l[idx]), 1e-12)
    body_ratio = abs(float(c[idx]) - float(o[idx])) / candle_range
    close_pos = clamp((float(c[idx]) - float(l[idx])) / candle_range, 0.0, 1.0)
    return body_ratio, close_pos


def _trigger(direction: str, tf: dict) -> tuple[bool, str, dict]:
    """Validate a *fresh* structural breakout, retest and immediate re-acceleration.

    The current closed 5m candle must be the confirmation candle.  The breakout
    must have happened only 1-3 retest candles earlier; there is no allowance for
    several continuation candles before entry.  This prevents buying/selling the
    third developed candle of an already-consumed impulse.
    """
    o, h, l, c, v = tf["o"], tf["h"], tf["l"], tf["c"], tf.get("v", [])
    ema20 = tf["ema20"]
    atr_value = float(tf["atr"])
    if len(c) < max(EMA_SLOW + STRUCTURE_LOOKBACK_BARS + 6, 240):
        return False, "NOT_ENOUGH_BARS", {}
    if atr_value <= 0:
        return False, "INVALID_ATR", {}

    i = len(c) - 1
    confirm_body_ratio, confirm_close_pos = _candle_shape(o, h, l, c, i)
    confirm_rvol = relative_volume(v, i) if v else 1.0
    ema_extension_atr = abs(float(c[i]) - float(ema20[i])) / atr_value

    base_diag = {
        "trigger_body_ratio": confirm_body_ratio,
        "trigger_close_pos": confirm_close_pos,
        "trigger_rvol": confirm_rvol,
        "extension_atr": ema_extension_atr,
    }

    if direction == "long":
        if not (
            float(c[i]) > float(o[i])
            and confirm_body_ratio >= TRIGGER_MIN_BODY_RATIO
            and confirm_close_pos >= TRIGGER_CLOSE_POS_LONG_MIN
            and confirm_rvol >= TRIGGER_MIN_RVOL
        ):
            return False, "NO_FRESH_CONFIRMATION", base_diag
    else:
        if not (
            float(c[i]) < float(o[i])
            and confirm_body_ratio >= TRIGGER_MIN_BODY_RATIO
            and confirm_close_pos <= TRIGGER_CLOSE_POS_SHORT_MAX
            and confirm_rvol >= TRIGGER_MIN_RVOL
        ):
            return False, "NO_FRESH_CONFIRMATION", base_diag

    # Current candle must follow the retest immediately.  Search for the most
    # recent valid breakout whose intervening 1-3 candles form the retest.
    last_diag = dict(base_diag)
    for retest_count in range(1, RETEST_MAX_BARS_AFTER_BREAKOUT + 1):
        breakout_idx = i - retest_count - 1
        if breakout_idx <= STRUCTURE_LOOKBACK_BARS:
            continue
        structure_start = breakout_idx - STRUCTURE_LOOKBACK_BARS
        structure_end = breakout_idx
        if direction == "long":
            structural_level = max(float(x) for x in h[structure_start:structure_end])
        else:
            structural_level = min(float(x) for x in l[structure_start:structure_end])

        breakout_body_ratio, breakout_close_pos = _candle_shape(o, h, l, c, breakout_idx)
        breakout_rvol = relative_volume(v, breakout_idx) if v else 1.0
        breakout_buffer = atr_value * BREAKOUT_CLOSE_BUFFER_ATR
        breakout_extension_atr = abs(float(c[breakout_idx]) - structural_level) / atr_value
        retest_indices = list(range(breakout_idx + 1, i))
        if not retest_indices:
            continue

        if direction == "long":
            breakout_ok = (
                float(c[breakout_idx]) > float(o[breakout_idx])
                and float(c[breakout_idx]) >= structural_level + breakout_buffer
                and breakout_body_ratio >= BREAKOUT_MIN_BODY_RATIO
                and breakout_close_pos >= BREAKOUT_CLOSE_POS_LONG_MIN
                and breakout_rvol >= BREAKOUT_MIN_RVOL
                and breakout_extension_atr <= BREAKOUT_MAX_EXTENSION_ATR
            )
            retest_low = min(float(l[j]) for j in retest_indices)
            retest_extreme = retest_low
            retest_touch = retest_low <= structural_level + atr_value * RETEST_TOUCH_TOL_ATR
            no_deep_wick = retest_low >= structural_level - atr_value * RETEST_MAX_PENETRATION_ATR
            no_failed_close = all(
                float(c[j]) >= structural_level - atr_value * RETEST_CLOSE_INVALIDATION_ATR
                for j in retest_indices
            )
            retest_stayed_near = all(
                abs(float(c[j]) - structural_level) <= atr_value * RETEST_MAX_CLOSE_DISTANCE_ATR
                for j in retest_indices
            )
            last_retest_idx = retest_indices[-1]
            last_retest_near = (
                float(l[last_retest_idx]) <= structural_level + atr_value * RETEST_TOUCH_TOL_ATR
                and abs(float(c[last_retest_idx]) - structural_level) <= atr_value * RETEST_MAX_CLOSE_DISTANCE_ATR
            )
            confirm_level = max(structural_level, float(h[last_retest_idx]))
            confirm_ok = (
                float(c[i]) >= confirm_level + atr_value * CONFIRM_BREAK_BUFFER_ATR
                and float(c[i]) > structural_level
            )
            structure_extension_atr = (float(c[i]) - structural_level) / atr_value
        else:
            breakout_ok = (
                float(c[breakout_idx]) < float(o[breakout_idx])
                and float(c[breakout_idx]) <= structural_level - breakout_buffer
                and breakout_body_ratio >= BREAKOUT_MIN_BODY_RATIO
                and breakout_close_pos <= BREAKOUT_CLOSE_POS_SHORT_MAX
                and breakout_rvol >= BREAKOUT_MIN_RVOL
                and breakout_extension_atr <= BREAKOUT_MAX_EXTENSION_ATR
            )
            retest_high = max(float(h[j]) for j in retest_indices)
            retest_extreme = retest_high
            retest_touch = retest_high >= structural_level - atr_value * RETEST_TOUCH_TOL_ATR
            no_deep_wick = retest_high <= structural_level + atr_value * RETEST_MAX_PENETRATION_ATR
            no_failed_close = all(
                float(c[j]) <= structural_level + atr_value * RETEST_CLOSE_INVALIDATION_ATR
                for j in retest_indices
            )
            retest_stayed_near = all(
                abs(float(c[j]) - structural_level) <= atr_value * RETEST_MAX_CLOSE_DISTANCE_ATR
                for j in retest_indices
            )
            last_retest_idx = retest_indices[-1]
            last_retest_near = (
                float(h[last_retest_idx]) >= structural_level - atr_value * RETEST_TOUCH_TOL_ATR
                and abs(float(c[last_retest_idx]) - structural_level) <= atr_value * RETEST_MAX_CLOSE_DISTANCE_ATR
            )
            confirm_level = min(structural_level, float(l[last_retest_idx]))
            confirm_ok = (
                float(c[i]) <= confirm_level - atr_value * CONFIRM_BREAK_BUFFER_ATR
                and float(c[i]) < structural_level
            )
            structure_extension_atr = (structural_level - float(c[i])) / atr_value

        last_diag = {
            **base_diag,
            "structural_level": structural_level,
            "breakout_idx": breakout_idx,
            "breakout_age_bars": i - breakout_idx,
            "retest_bars": retest_count,
            "retest_extreme": retest_extreme,
            "breakout_body_ratio": breakout_body_ratio,
            "breakout_close_pos": breakout_close_pos,
            "breakout_rvol": breakout_rvol,
            "breakout_extension_atr": breakout_extension_atr,
            "structure_extension_atr": structure_extension_atr,
            "confirm_level": confirm_level,
            "retest_touch": retest_touch,
            "retest_no_deep_wick": no_deep_wick,
            "retest_no_failed_close": no_failed_close,
            "retest_stayed_near": retest_stayed_near,
            "last_retest_near": last_retest_near,
        }
        if not breakout_ok:
            continue
        if not (retest_touch and no_deep_wick and no_failed_close and retest_stayed_near and last_retest_near):
            continue
        if not confirm_ok:
            continue
        if structure_extension_atr > ENTRY_MAX_STRUCTURE_EXTENSION_ATR:
            return False, "IMPULSE_ALREADY_EXTENDED", last_diag
        return True, "OK", last_diag

    return False, "NO_FRESH_BREAKOUT_RETEST", last_diag


def _structure_target(direction, close, highs5, lows5, highs15, lows15):
    """Nearest opposing swing, or a projection of the latest consolidation range."""
    recent_high = max(highs5[-8:-1])
    recent_low = min(lows5[-8:-1])
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

        retest_extreme = float(trigger_diag.get("retest_extreme", close5))
        if bias1h == "long":
            direction = Direction.LONG
            structural_pct = max(0.0, (close5 - retest_extreme) / max(close5, 1e-12))
            stop = retest_extreme - atr5 * MTF_SL_BUFFER_ATR
        else:
            direction = Direction.SHORT
            structural_pct = max(0.0, (retest_extreme - close5) / max(close5, 1e-12))
            stop = retest_extreme + atr5 * MTF_SL_BUFFER_ATR

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
        freshness_quality = clamp(
            1.0 - (float(trigger_diag.get("retest_bars", RETEST_MAX_BARS_AFTER_BREAKOUT)) - 1.0)
            / max(RETEST_MAX_BARS_AFTER_BREAKOUT, 1),
            0.0,
            1.0,
        )
        extension_quality = clamp(
            1.0 - float(trigger_diag.get("structure_extension_atr", ENTRY_MAX_STRUCTURE_EXTENSION_ATR))
            / max(ENTRY_MAX_STRUCTURE_EXTENSION_ATR, 1e-12),
            0.0,
            1.0,
        )
        quality = clamp(
            0.27 * h1_strength
            + 0.23 * m15_strength
            + 0.18 * m5_strength
            + 0.14 * trend_alignment_quality
            + 0.10 * freshness_quality
            + 0.08 * extension_quality,
            0.0,
            1.0,
        )
        score = round(min(MAX_SCORE, 55.0 + 45.0 * quality), 2)
        if score < MIN_SCORE_TO_SIGNAL:
            return self._reject("score_too_low", score=score, min=MIN_SCORE_TO_SIGNAL)

        structural_target = _structure_target(direction, close5, tf5['h'], tf5['l'], tf15['h'], tf15['l'])
        structural_level = float(trigger_diag.get("structural_level", close5))
        path_from_breakout = abs(structural_target - structural_level)
        path_consumed = abs(close5 - structural_level)
        consumed_ratio = path_consumed / max(path_from_breakout, 1e-12)
        # Only use the ratio gate when the remaining structural path is meaningful.
        if path_from_breakout >= atr5 * 0.45 and consumed_ratio > MAX_IMPULSE_CONSUMED_RATIO:
            return self._reject(
                "impulse_already_consumed",
                impulse_consumed_ratio=consumed_ratio,
                max=MAX_IMPULSE_CONSUMED_RATIO,
                structural_level=structural_level,
                structural_target=structural_target,
                entry=close5,
            )

        target, target_ratio = front_run_target(close5, structural_target, direction)
        structural_tp_pct = abs(structural_target - close5) / close5
        tp_pct = abs(target - close5) / close5
        execution_rr = tp_pct / sl_pct
        if execution_rr < MIN_RR_TO_SIGNAL:
            return self._reject("rr_too_low", execution_rr=execution_rr, min=MIN_RR_TO_SIGNAL)
        if target <= 0:
            return self._reject('invalid_structural_target')
        strength = clamp(score / 100.0, STRENGTH_MIN, STRENGTH_MAX)
        be_activation, be_offset, bucket = _break_even(score, strength, sl_pct, tp_pct)
        be_ratio = break_even_activation_ratio()
        lock_activation = profit_lock_activation_ratio()
        lock_capture = profit_lock_capture_ratio()

        self.last_trace = {
            "accepted": True,
            "reason": "fresh_breakout_retest_confirmed",
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
            "structural_target": structural_target,
            "structural_level": structural_level,
            "target_front_run_ratio": target_ratio,
            "execution_rr": execution_rr,
            "structural_stop_pct": structural_pct,
            "trigger_body_ratio": trigger_diag.get("trigger_body_ratio"),
            "trigger_close_pos": trigger_diag.get("trigger_close_pos"),
            "breakout_age_bars": trigger_diag.get("breakout_age_bars"),
            "retest_bars": trigger_diag.get("retest_bars"),
            "structure_extension_atr": trigger_diag.get("structure_extension_atr"),
            "impulse_consumed_ratio": consumed_ratio,
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
            ("mtf_1h_15m_alignment", "structural_breakout", "fresh_retest", "fresh_confirmation"),
            {
                "strategy_model": "structural_breakout_retest_fresh_confirmation_v4",
                "score": score,
                "strength": strength,
                "atr_pct": atr_pct,
                "atr_value": atr5,
                "structural_level": structural_level,
                "breakout_age_bars": trigger_diag.get("breakout_age_bars"),
                "retest_bars": trigger_diag.get("retest_bars"),
                "retest_extreme": retest_extreme,
                "breakout_body_ratio": trigger_diag.get("breakout_body_ratio"),
                "breakout_rvol": trigger_diag.get("breakout_rvol"),
                "breakout_extension_atr": trigger_diag.get("breakout_extension_atr"),
                "trigger_body_ratio": trigger_diag.get("trigger_body_ratio"),
                "trigger_close_pos": trigger_diag.get("trigger_close_pos"),
                "trigger_rvol": trigger_diag.get("trigger_rvol"),
                "trigger_extension_atr": extension_atr,
                "structure_extension_atr": trigger_diag.get("structure_extension_atr"),
                "impulse_consumed_ratio": consumed_ratio,
                "adx5": adx5,
                "adx15": diag15["adx"],
                "adx1h": diag1h["adx"],
                "sl_pct": sl_pct,
                "tp_pct": tp_pct,
                "structural_tp_pct": structural_tp_pct,
                "structural_target_price": structural_target,
                "target_front_run_ratio": target_ratio,
                "execution_rr": execution_rr,
                "structural_stop_pct": structural_pct,
                "partial_tp_enabled": False,
                "tp2_price": target,
                "break_even_activation_pct": be_activation,
                "break_even_offset_pct": be_offset,
                "break_even_activation_ratio": be_ratio,
                "profit_lock_activation_ratio": lock_activation,
                "profit_lock_capture_ratio": lock_capture,
                "management_bucket": bucket,
            },
        )
