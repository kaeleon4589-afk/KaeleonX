from __future__ import annotations

from app.models.enums import Direction, Strategy
from app.strategy.source_math import extract, atr, ema, relative_volume
from app.strategy.v2_common import (
    StrategyCandidate, candle_shape, clamp, enforce_stop_distance, env_float,
    frames, swing_barrier, target_from_structure, trend_alignment,
)


class BreakoutRetestStrategyV2:
    """Fresh breakout -> retest -> continuation model for KAELEON.

    Discovery is done on CLOSED 5m candles.  15m/1h are context only.  A valid
    structure becomes ARMED and live/1m confirmation is delegated to the common
    entry lifecycle engine.
    """

    def __init__(self):
        self.last_trace: dict = {}

    def _reject(self, reason: str, **metrics):
        self.last_trace = {"accepted": False, "reason": reason, **metrics}
        return None

    def scan(self, regime, snapshot, *, allow_watch: bool = True):
        self.last_trace = {"accepted": False, "reason": "not_evaluated"}
        if getattr(regime, "hard_block", False) or not getattr(regime, "breakout_allowed", False):
            return self._reject("regime_breakout_not_allowed")
        direction = getattr(regime, "direction", Direction.NEUTRAL)
        if direction not in {Direction.LONG, Direction.SHORT}:
            return self._reject("trend_direction_unavailable")

        tf = frames(snapshot, getattr(snapshot, "candles", None))
        c5 = list(tf.get("5m") or [])
        c15 = list(tf.get("15m") or [])
        c1h = list(tf.get("1h") or [])
        if len(c5) < 80 or len(c15) < 80 or len(c1h) < 80:
            return self._reject("insufficient_multitimeframe_bars", bars5=len(c5), bars15=len(c15), bars1h=len(c1h))

        o, h, l, c, v = extract(c5)
        atr_value = float(atr(h, l, c, 14))
        if atr_value <= 0:
            return self._reject("invalid_atr")
        current = float(c[-1])

        side = "long" if direction == Direction.LONG else "short"
        d15, a15, _ = trend_alignment(c15, slow=min(200, max(50, len(c15) - 5)))
        d1h, a1h, _ = trend_alignment(c1h, slow=min(200, max(50, len(c1h) - 5)))
        expected = side
        if d15 != expected or d1h != expected:
            return self._reject("htf_alignment_lost", direction15=d15, direction1h=d1h)

        lookback = 24
        max_breakout_age = int(env_float("V2_BREAKOUT_MAX_AGE_BARS", 6, 3, 12))
        candidates = []
        n = len(c5)
        start = max(lookback, n - max_breakout_age - 2)
        for idx in range(start, n):
            if idx <= lookback:
                continue
            prior_h = max(float(x) for x in h[idx - lookback:idx])
            prior_l = min(float(x) for x in l[idx - lookback:idx])
            shape = candle_shape(c5[idx])
            rv = float(relative_volume(v, idx, 20))
            if direction == Direction.LONG:
                level = prior_h
                broke = float(c[idx]) >= level + 0.04 * atr_value
                close_pos_ok = shape["close_pos"] >= 0.62
                extension = (float(c[idx]) - level) / atr_value
            else:
                level = prior_l
                broke = float(c[idx]) <= level - 0.04 * atr_value
                close_pos_ok = shape["close_pos"] <= 0.38
                extension = (level - float(c[idx])) / atr_value
            if not broke:
                continue
            if shape["body_ratio"] < 0.42 or not close_pos_ok or rv < 0.80:
                continue
            if shape["range"] > 2.3 * atr_value or extension > 1.35:
                continue
            breakout_score = (
                30.0 * clamp(shape["body_ratio"] / 0.75, 0.0, 1.0)
                + 18.0 * clamp(rv / 1.60, 0.0, 1.0)
                + 12.0 * clamp(1.0 - max(0.0, extension - 0.35) / 1.0, 0.0, 1.0)
            )
            candidates.append((idx, level, breakout_score, rv, shape, extension))

        if not candidates:
            return self._reject("no_fresh_breakout")

        # Prefer the newest structure; score breaks ties.
        breakout_idx, level, breakout_score, breakout_rvol, breakout_shape, breakout_extension = max(
            candidates, key=lambda item: (item[0], item[2])
        )
        age = n - 1 - breakout_idx
        if age > max_breakout_age:
            return self._reject("breakout_stale", age=age)

        retest_tolerance = 0.28 * atr_value
        invalidation_buffer = 0.48 * atr_value
        retest_idx = None
        retest_extreme = None
        retest_quality = 0.0
        for idx in range(breakout_idx + 1, n):
            shape = candle_shape(c5[idx])
            if direction == Direction.LONG:
                if float(l[idx]) < level - invalidation_buffer:
                    return self._reject("breakout_failed_below_level", level=level, low=float(l[idx]))
                touched = float(l[idx]) <= level + retest_tolerance
                if touched:
                    retest_idx = idx
                    retest_extreme = float(l[idx])
                    distance = abs(float(l[idx]) - level) / atr_value
                    retest_quality = 20.0 * clamp(1.0 - distance / 0.60, 0.0, 1.0) + 10.0 * shape["lower_wick_ratio"]
            else:
                if float(h[idx]) > level + invalidation_buffer:
                    return self._reject("breakout_failed_above_level", level=level, high=float(h[idx]))
                touched = float(h[idx]) >= level - retest_tolerance
                if touched:
                    retest_idx = idx
                    retest_extreme = float(h[idx])
                    distance = abs(float(h[idx]) - level) / atr_value
                    retest_quality = 20.0 * clamp(1.0 - distance / 0.60, 0.0, 1.0) + 10.0 * shape["upper_wick_ratio"]

        if retest_idx is None:
            distance_now = abs(current - level) / atr_value
            if allow_watch and distance_now <= 1.30:
                zone_low = level - 0.22 * atr_value
                zone_high = level + 0.32 * atr_value
                raw_stop = level - 0.70 * atr_value if direction == Direction.LONG else level + 0.70 * atr_value
                stop_info = enforce_stop_distance(current, raw_stop, atr_value, direction, min_atr=0.55, max_atr=1.80)
                if stop_info is None:
                    return self._reject("watch_stop_geometry_invalid")
                stop, risk = stop_info
                structural = swing_barrier(c15, direction, current) or (
                    current + risk * 1.50 if direction == Direction.LONG else current - risk * 1.50
                )
                target_info = target_from_structure(current, stop, structural, direction, target_rr=1.50)
                if target_info is None:
                    return self._reject("watch_target_geometry_invalid")
                target, rr = target_info
                quality = clamp(58.0 + breakout_score * 0.35 + min(a15, a1h) * 15.0 - distance_now * 4.0, 0.0, 100.0)
                candidate = StrategyCandidate(
                    Strategy.BREAKOUT_RETEST, direction, "WATCH", quality, current, level,
                    level - invalidation_buffer if direction == Direction.LONG else level + invalidation_buffer,
                    stop, target, structural, zone_low, zone_high, int(c5[breakout_idx].timestamp),
                    ("fresh_breakout", "waiting_retest"),
                    {
                        "strategy_model": "breakout_retest_v2",
                        "structure_level": level,
                        "breakout_idx": breakout_idx,
                        "breakout_age_bars": age,
                        "breakout_rvol": breakout_rvol,
                        "breakout_body_ratio": breakout_shape["body_ratio"],
                        "breakout_extension_atr": breakout_extension,
                        "atr_value": atr_value,
                        "signal_entry_price": current,
                        "sl_pct": abs(current - stop) / current,
                        "execution_rr": rr,
                        "structural_target_price": structural,
                        "target_rr_cap": 1.50,
                    },
                )
                self.last_trace = {"accepted": True, "reason": "watch_waiting_retest", "stage": "WATCH", "quality": quality, "level": level, "side": direction.value}
                return candidate
            return self._reject("breakout_retest_not_reached", distance_atr=distance_now)

        # A retest exists.  Require a fresh continuation candle, but allow the
        # retest candle itself if it closed strongly back through the level.
        confirm_idx = None
        for idx in range(retest_idx, n):
            shape = candle_shape(c5[idx])
            if direction == Direction.LONG:
                confirmed = float(c[idx]) > level and float(c[idx]) >= float(o[idx]) and shape["close_pos"] >= 0.54 and shape["body_ratio"] >= 0.22
            else:
                confirmed = float(c[idx]) < level and float(c[idx]) <= float(o[idx]) and shape["close_pos"] <= 0.46 and shape["body_ratio"] >= 0.22
            if confirmed:
                confirm_idx = idx
                break

        if confirm_idx is None:
            if allow_watch:
                raw_stop = float(retest_extreme) - 0.16 * atr_value if direction == Direction.LONG else float(retest_extreme) + 0.16 * atr_value
                stop_info = enforce_stop_distance(current, raw_stop, atr_value, direction, min_atr=0.55, max_atr=1.80)
                if stop_info is None:
                    return self._reject("retest_stop_geometry_invalid")
                stop, risk = stop_info
                structural = swing_barrier(c15, direction, current) or (current + risk * 1.50 if direction == Direction.LONG else current - risk * 1.50)
                target_info = target_from_structure(current, stop, structural, direction, target_rr=1.50)
                if target_info is None:
                    return self._reject("retest_target_geometry_invalid")
                target, rr = target_info
                quality = clamp(61.0 + breakout_score * 0.30 + retest_quality * 0.55 + min(a15, a1h) * 10.0, 0.0, 100.0)
                zone_low = level - 0.18 * atr_value
                zone_high = level + 0.45 * atr_value
                candidate = StrategyCandidate(
                    Strategy.BREAKOUT_RETEST, direction, "WATCH", quality, current, level,
                    level - invalidation_buffer if direction == Direction.LONG else level + invalidation_buffer,
                    stop, target, structural, zone_low, zone_high, int(c5[breakout_idx].timestamp),
                    ("fresh_breakout", "retest_touched", "waiting_continuation"),
                    {
                        "strategy_model": "breakout_retest_v2",
                        "structure_level": level,
                        "breakout_idx": breakout_idx,
                        "retest_idx": retest_idx,
                        "atr_value": atr_value,
                        "signal_entry_price": current,
                        "sl_pct": abs(current - stop) / current,
                        "execution_rr": rr,
                        "structural_target_price": structural,
                        "target_rr_cap": 1.50,
                    },
                )
                self.last_trace = {"accepted": True, "reason": "watch_waiting_continuation", "stage": "WATCH", "quality": quality, "level": level, "side": direction.value}
                return candidate
            return self._reject("retest_without_confirmation")

        confirm_age = n - 1 - confirm_idx
        if confirm_age > 2:
            return self._reject("continuation_confirmation_stale", age=confirm_age)
        extension_now = (current - level) / atr_value if direction == Direction.LONG else (level - current) / atr_value
        if extension_now > 0.95:
            return self._reject("entry_chased_after_retest", extension_atr=extension_now)

        raw_stop = float(retest_extreme) - 0.16 * atr_value if direction == Direction.LONG else float(retest_extreme) + 0.16 * atr_value
        stop_info = enforce_stop_distance(current, raw_stop, atr_value, direction, min_atr=0.55, max_atr=1.80)
        if stop_info is None:
            return self._reject("stop_geometry_invalid")
        stop, risk = stop_info
        structural = swing_barrier(c15, direction, current) or (current + risk * 1.50 if direction == Direction.LONG else current - risk * 1.50)
        target_rr = env_float("TRADE_BREAKOUT_RETEST_TARGET_RR", 1.50, 1.05, 1.80)
        target_info = target_from_structure(current, stop, structural, direction, target_rr=target_rr)
        if target_info is None:
            return self._reject("target_geometry_invalid")
        target, rr = target_info

        confirm_shape = candle_shape(c5[confirm_idx])
        freshness = 1.0 - confirm_age / 3.0
        quality = clamp(
            42.0
            + breakout_score * 0.34
            + retest_quality * 0.46
            + min(a15, a1h) * 10.0
            + confirm_shape["body_ratio"] * 8.0
            + freshness * 7.0,
            0.0, 100.0,
        )
        min_score = env_float("V2_BREAKOUT_MIN_SCORE", 68.0, 50.0, 90.0)
        if quality < min_score:
            return self._reject("quality_below_threshold", quality=quality, minimum=min_score)

        trigger = float(c5[confirm_idx].high if direction == Direction.LONG else c5[confirm_idx].low)
        zone_low = min(level - 0.12 * atr_value, current - 0.12 * atr_value)
        zone_high = max(level + 0.55 * atr_value, current + 0.12 * atr_value)
        candidate = StrategyCandidate(
            Strategy.BREAKOUT_RETEST, direction, "READY", quality, current, trigger,
            level - invalidation_buffer if direction == Direction.LONG else level + invalidation_buffer,
            stop, target, structural, zone_low, zone_high, int(c5[breakout_idx].timestamp),
            ("fresh_breakout", "retest_confirmed", "continuation_confirmed"),
            {
                "strategy_model": "breakout_retest_v2",
                "structure_level": level,
                "breakout_idx": breakout_idx,
                "retest_idx": retest_idx,
                "confirmation_idx": confirm_idx,
                "breakout_age_bars": age,
                "breakout_rvol": breakout_rvol,
                "breakout_body_ratio": breakout_shape["body_ratio"],
                "breakout_extension_atr": breakout_extension,
                "confirmation_body_ratio": confirm_shape["body_ratio"],
                "atr_value": atr_value,
                "signal_entry_price": current,
                "sl_pct": abs(current - stop) / current,
                "tp_pct": abs(target - current) / current,
                "execution_rr": rr,
                "structural_rr_estimate": abs(structural - current) / risk,
                "structural_target_price": structural,
                "target_rr_cap": target_rr,
            },
        )
        self.last_trace = {
            "accepted": True, "reason": "setup_ready", "stage": "READY", "quality": quality,
            "side": direction.value, "level": level, "entry": current, "stop": stop, "target": target,
            "execution_rr": rr, "breakout_age_bars": age,
        }
        return candidate
