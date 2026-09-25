from __future__ import annotations

from app.models.enums import Direction, Strategy
from app.models.trading import TradeIntent
from app.position.protection import (
    break_even_activation_ratio, front_run_target,
    profit_lock_activation_ratio, profit_lock_capture_ratio,
)
from app.strategy.source_math import atr, candle_quality, clamp, ema, extract, relative_volume

EMA_FAST = 20
EMA_MID = 50
MIN_CANDLES_REQUIRED = 260
MIN_NONZERO_VOLUME_RATIO = 0.92
SWEEP_LOOKBACK = 34
SWEEP_MAX_AGE_BARS = 8
SWEEP_MIN_DEPTH_ATR = 0.10
SWEEP_MIN_WICK_RATIO = 0.28
SWEEP_MIN_RVOL = 0.58
SWEEP_RECOVER_TOL_ATR = 0.36
TRIGGER_MIN_RVOL = 0.48
TRIGGER_MIN_BODY_RATIO = 0.12
TRIGGER_CLOSE_POS_LONG_MIN = 0.46
TRIGGER_CLOSE_POS_SHORT_MAX = 0.54
TRIGGER_EXTENSION_MAX_ATR = 2.25
TRIGGER_EMA20_RECOVER_TOL_ATR = 0.55
TRIGGER_EMA50_RECOVER_TOL_ATR = 1.05
RETEST_INVALIDATION_ATR = 0.58
SL_BUFFER_ATR = 0.18
TARGET_LOOKBACK = 48
MIN_RR = 0.95
ATR_PCT_MIN = 0.0013
ATR_PCT_MAX = 0.0280
MIN_SCORE = 74.0


def _body_ratio(o: float, h: float, l: float, c: float) -> float:
    return abs(c - o) / max(h - l, 1e-12)


def _close_pos(h: float, l: float, c: float) -> float:
    return clamp((c - l) / max(h - l, 1e-12), 0.0, 1.0)


def _lower_wick(o: float, h: float, l: float, c: float) -> float:
    return clamp((min(o, c) - l) / max(h - l, 1e-12), 0.0, 1.0)


def _upper_wick(o: float, h: float, l: float, c: float) -> float:
    return clamp((h - max(o, c)) / max(h - l, 1e-12), 0.0, 1.0)


def _score_candidate(*, sweep_depth_atr: float, sweep_wick_ratio: float, sweep_rvol: float,
                     trigger_rvol: float, trigger_body_ratio: float, trigger_close_pos: float,
                     extension_atr: float, rr_estimate: float, bars_since_sweep: int) -> float:
    depth_q = clamp((sweep_depth_atr - SWEEP_MIN_DEPTH_ATR) / 0.70, 0.0, 1.0)
    wick_q = clamp((sweep_wick_ratio - SWEEP_MIN_WICK_RATIO) / 0.42, 0.0, 1.0)
    sweep_rvol_q = clamp((sweep_rvol - 0.85) / 0.90, 0.0, 1.0)
    trigger_rvol_q = clamp((trigger_rvol - 0.80) / 0.85, 0.0, 1.0)
    body_q = clamp((trigger_body_ratio - TRIGGER_MIN_BODY_RATIO) / 0.45, 0.0, 1.0)
    close_q = clamp((trigger_close_pos - 0.50) / 0.40, 0.0, 1.0)
    extension_penalty = clamp(extension_atr / TRIGGER_EXTENSION_MAX_ATR, 0.0, 1.0)
    rr_q = clamp((rr_estimate - MIN_RR) / 1.4, 0.0, 1.0)
    age_penalty = clamp((bars_since_sweep - 1) / max(SWEEP_MAX_AGE_BARS - 1, 1), 0.0, 1.0)
    quality = clamp(
        0.19 * depth_q
        + 0.16 * wick_q
        + 0.12 * sweep_rvol_q
        + 0.13 * trigger_rvol_q
        + 0.14 * body_q
        + 0.10 * close_q
        + 0.18 * rr_q
        - 0.08 * extension_penalty
        - 0.06 * age_penalty,
        0.0,
        1.0,
    )
    return round(min(100.0, 66.0 + 34.0 * quality), 2)


def _detect(direction: str, *, o, h, l, c, v, ema20, ema50, atr_value):
    if len(c) < max(SWEEP_LOOKBACK + SWEEP_MAX_AGE_BARS + 6, 90):
        return None
    trigger_idx = len(c) - 1
    best = None
    for sweep_idx in range(max(SWEEP_LOOKBACK + 2, trigger_idx - SWEEP_MAX_AGE_BARS), trigger_idx):
        left_start = max(0, sweep_idx - SWEEP_LOOKBACK)
        if sweep_idx - left_start < 12:
            continue
        bars_since = trigger_idx - sweep_idx
        if bars_since <= 0 or bars_since > SWEEP_MAX_AGE_BARS:
            continue
        trigger_rvol = relative_volume(v, trigger_idx, 24)
        trigger_body = _body_ratio(o[trigger_idx], h[trigger_idx], l[trigger_idx], c[trigger_idx])
        trigger_close = _close_pos(h[trigger_idx], l[trigger_idx], c[trigger_idx])

        if direction == "long":
            level = min(l[left_start:sweep_idx])
            sweep_extreme = float(l[sweep_idx])
            depth = (float(level) - sweep_extreme) / max(atr_value, 1e-12)
            wick = _lower_wick(o[sweep_idx], h[sweep_idx], l[sweep_idx], c[sweep_idx])
            sweep_rvol = relative_volume(v, sweep_idx, 24)
            sweep_close = _close_pos(h[sweep_idx], l[sweep_idx], c[sweep_idx])
            if depth < SWEEP_MIN_DEPTH_ATR or wick < SWEEP_MIN_WICK_RATIO or sweep_rvol < SWEEP_MIN_RVOL:
                continue
            if float(c[sweep_idx]) < float(level) - atr_value * SWEEP_RECOVER_TOL_ATR or sweep_close < 0.46:
                continue
            invalidation = min(l[sweep_idx + 1:trigger_idx + 1])
            if invalidation < sweep_extreme - atr_value * RETEST_INVALIDATION_ATR:
                continue
            extension = (float(c[trigger_idx]) - float(level)) / max(atr_value, 1e-12)
            trigger_ok = (
                float(c[trigger_idx]) > float(level)
                and float(c[trigger_idx]) >= float(c[sweep_idx])
                and float(c[trigger_idx]) >= float(ema20[trigger_idx]) - atr_value * TRIGGER_EMA20_RECOVER_TOL_ATR
                and float(c[trigger_idx]) >= float(ema50[trigger_idx]) - atr_value * TRIGGER_EMA50_RECOVER_TOL_ATR
                and trigger_rvol >= TRIGGER_MIN_RVOL
                and trigger_body >= TRIGGER_MIN_BODY_RATIO
                and trigger_close >= TRIGGER_CLOSE_POS_LONG_MIN
                and extension <= TRIGGER_EXTENSION_MAX_ATR
            )
            if not trigger_ok:
                continue
            target_start = max(0, sweep_idx - TARGET_LOOKBACK)
            target_level = max(h[target_start:sweep_idx]) if sweep_idx > target_start else max(h[:sweep_idx] or [0.0])
            stop_price = sweep_extreme - atr_value * SL_BUFFER_ATR
            risk_abs = max(float(c[trigger_idx]) - stop_price, 1e-12)
            reward_abs = max(float(target_level) - float(c[trigger_idx]), 0.0)
            score_close = trigger_close
        else:
            level = max(h[left_start:sweep_idx])
            sweep_extreme = float(h[sweep_idx])
            depth = (sweep_extreme - float(level)) / max(atr_value, 1e-12)
            wick = _upper_wick(o[sweep_idx], h[sweep_idx], l[sweep_idx], c[sweep_idx])
            sweep_rvol = relative_volume(v, sweep_idx, 24)
            sweep_close = _close_pos(h[sweep_idx], l[sweep_idx], c[sweep_idx])
            if depth < SWEEP_MIN_DEPTH_ATR or wick < SWEEP_MIN_WICK_RATIO or sweep_rvol < SWEEP_MIN_RVOL:
                continue
            if float(c[sweep_idx]) > float(level) + atr_value * SWEEP_RECOVER_TOL_ATR or sweep_close > 0.54:
                continue
            invalidation = max(h[sweep_idx + 1:trigger_idx + 1])
            if invalidation > sweep_extreme + atr_value * RETEST_INVALIDATION_ATR:
                continue
            extension = (float(level) - float(c[trigger_idx])) / max(atr_value, 1e-12)
            trigger_ok = (
                float(c[trigger_idx]) < float(level)
                and float(c[trigger_idx]) <= float(c[sweep_idx])
                and float(c[trigger_idx]) <= float(ema20[trigger_idx]) + atr_value * TRIGGER_EMA20_RECOVER_TOL_ATR
                and float(c[trigger_idx]) <= float(ema50[trigger_idx]) + atr_value * TRIGGER_EMA50_RECOVER_TOL_ATR
                and trigger_rvol >= TRIGGER_MIN_RVOL
                and trigger_body >= TRIGGER_MIN_BODY_RATIO
                and trigger_close <= TRIGGER_CLOSE_POS_SHORT_MAX
                and extension <= TRIGGER_EXTENSION_MAX_ATR
            )
            if not trigger_ok:
                continue
            target_start = max(0, sweep_idx - TARGET_LOOKBACK)
            target_level = min(l[target_start:sweep_idx]) if sweep_idx > target_start else min(l[:sweep_idx] or [0.0])
            stop_price = sweep_extreme + atr_value * SL_BUFFER_ATR
            risk_abs = max(stop_price - float(c[trigger_idx]), 1e-12)
            reward_abs = max(float(c[trigger_idx]) - float(target_level), 0.0)
            score_close = 1.0 - trigger_close

        rr_estimate = reward_abs / risk_abs if risk_abs > 0 else 0.0
        if rr_estimate < MIN_RR:
            continue
        score = _score_candidate(
            sweep_depth_atr=depth,
            sweep_wick_ratio=wick,
            sweep_rvol=sweep_rvol,
            trigger_rvol=trigger_rvol,
            trigger_body_ratio=trigger_body,
            trigger_close_pos=score_close,
            extension_atr=extension,
            rr_estimate=rr_estimate,
            bars_since_sweep=bars_since,
        )
        candidate = {
            "direction": direction,
            "liquidity_level": float(level),
            "sweep_idx": int(sweep_idx),
            "bars_since_sweep": int(bars_since),
            "sweep_depth_atr": float(depth),
            "sweep_wick_ratio": float(wick),
            "sweep_rvol": float(sweep_rvol),
            "trigger_rvol": float(trigger_rvol),
            "trigger_body_ratio": float(trigger_body),
            "trigger_close_pos": float(trigger_close),
            "trigger_extension_atr": float(extension),
            "rr_estimate": float(rr_estimate),
            "target_level": float(target_level),
            "stop_price": float(stop_price),
            "score": float(score),
        }
        if best is None or score > best["score"]:
            best = candidate
    return best


class LiquiditySweepStrategy:
    def __init__(self):
        self.last_trace = {}

    def _reject(self, reason, **metrics):
        self.last_trace = {"accepted": False, "reason": reason, **metrics}
        return None

    def evaluate(self, regime, candles, decision_id, symbol, timeframe, current_price=None, snapshot=None):
        self.last_trace = {"accepted": False, "reason": "not_evaluated"}
        if regime.hard_block:
            return self._reject("regime_hard_block")
        if not regime.sweep_allowed:
            return self._reject("regime_sweep_not_allowed")
        c5 = list((getattr(snapshot, "timeframes", {}) or {}).get("5m", candles) if snapshot is not None else candles)
        if len(c5) < MIN_CANDLES_REQUIRED:
            return self._reject("insufficient_bars", bars=len(c5), required=MIN_CANDLES_REQUIRED)
        ok, quality_diag = candle_quality(c5, MIN_CANDLES_REQUIRED, MIN_NONZERO_VOLUME_RATIO)
        if not ok:
            return self._reject("bad_5m_candle_quality", **quality_diag)

        o, h, l, c, v = extract(c5)
        close5 = float(c[-1])
        atr_value = atr(h, l, c, 14)
        atr_pct = atr_value / max(close5, 1e-12)
        if atr_pct < ATR_PCT_MIN:
            return self._reject("atr_too_low", atr_pct=atr_pct, min=ATR_PCT_MIN)
        if atr_pct > ATR_PCT_MAX:
            return self._reject("atr_too_high", atr_pct=atr_pct, max=ATR_PCT_MAX)
        ema20, ema50 = ema(c, EMA_FAST), ema(c, EMA_MID)

        long_candidate = _detect("long", o=o, h=h, l=l, c=c, v=v, ema20=ema20, ema50=ema50, atr_value=atr_value)
        short_candidate = _detect("short", o=o, h=h, l=l, c=c, v=v, ema20=ema20, ema50=ema50, atr_value=atr_value)
        if not long_candidate and not short_candidate:
            return self._reject("no_valid_sweep")
        candidate = max([x for x in (long_candidate, short_candidate) if x], key=lambda x: x["score"])
        score = float(candidate["score"])
        if score < MIN_SCORE:
            return self._reject("score_too_low", score=score, min=MIN_SCORE)

        direction = Direction.LONG if candidate["direction"] == "long" else Direction.SHORT
        structural_stop = float(candidate["stop_price"])
        sl_pct = abs(close5 - structural_stop) / max(close5, 1e-12)
        if structural_stop <= 0 or sl_pct <= 0:
            return self._reject('invalid_structural_stop')
        target_level = float(candidate["target_level"])
        structural_tp_pct = abs(target_level - close5) / max(close5, 1e-12) if target_level > 0 else 0.0
        stop = structural_stop
        target, target_ratio = front_run_target(close5, target_level, direction)
        tp_pct = abs(target - close5) / max(close5, 1e-12)
        execution_rr = tp_pct / sl_pct
        if target <= 0 or execution_rr < MIN_RR:
            return self._reject('invalid_structural_target', execution_rr=execution_rr)
        # Keep the legacy percentage metadata for diagnostics, while actual
        # position management now uses distance ratios and executes in PositionManager.
        be_activation = clamp(max(sl_pct * 0.46, tp_pct * 0.40), 0.0028, min(tp_pct * 0.56, 0.0039))
        be_offset = clamp(max(atr_pct * 0.06, 0.00050), 0.00050, 0.00095)
        be_ratio = break_even_activation_ratio()
        lock_activation = profit_lock_activation_ratio()
        lock_capture = profit_lock_capture_ratio()

        self.last_trace = {
            "accepted": True,
            "reason": "setup_valid",
            "score": score,
            "side": candidate["direction"],
            "sweep_level": candidate["liquidity_level"],
            "depth_atr": candidate["sweep_depth_atr"],
            "wick_ratio": candidate["sweep_wick_ratio"],
            "sweep_rvol": candidate["sweep_rvol"],
            "trigger_rvol": candidate["trigger_rvol"],
            "structural_rr": candidate["rr_estimate"],
            "execution_rr": execution_rr,
            "age": candidate["bars_since_sweep"],
            "sl_pct": sl_pct,
            "tp_pct": tp_pct,
            "entry": close5,
            "stop": stop,
            "target": target,
            "structural_target": target_level,
            "target_front_run_ratio": target_ratio,
        }
        return TradeIntent(
            decision_id,
            symbol,
            Strategy.LIQUIDITY_SWEEP,
            direction,
            close5,
            stop,
            target,
            score,
            regime.risk_multiplier,
            timeframe,
            ("liquidity_swept", "level_reclaimed", "trigger_confirmed"),
            {
                "strategy_model": "liquidity_sweep_reversal_5m_v1",
                "score": score,
                "sweep_level": candidate["liquidity_level"],
                "sweep_depth_atr": candidate["sweep_depth_atr"],
                "sweep_wick_ratio": candidate["sweep_wick_ratio"],
                "sweep_rvol": candidate["sweep_rvol"],
                "trigger_rvol": candidate["trigger_rvol"],
                "trigger_body_ratio": candidate["trigger_body_ratio"],
                "trigger_close_pos": candidate["trigger_close_pos"],
                "bars_since_sweep": candidate["bars_since_sweep"],
                "structural_rr_estimate": candidate["rr_estimate"],
                "structural_tp_pct": structural_tp_pct,
                "structural_target_price": target_level,
                "target_front_run_ratio": target_ratio,
                "execution_rr": execution_rr,
                "rr_estimate": candidate["rr_estimate"],
                "sl_pct": sl_pct,
                "tp_pct": tp_pct,
                "partial_tp_enabled": False,
                "tp2_price": target,
                "break_even_activation_pct": be_activation,
                "break_even_offset_pct": be_offset,
                "break_even_activation_ratio": be_ratio,
                "profit_lock_activation_ratio": lock_activation,
                "profit_lock_capture_ratio": lock_capture,
            },
        )
