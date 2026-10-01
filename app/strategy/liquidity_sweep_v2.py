from __future__ import annotations

from app.models.enums import Direction, Strategy
from app.strategy.source_math import extract, atr, relative_volume
from app.strategy.v2_common import (
    StrategyCandidate, candle_shape, clamp, enforce_stop_distance, env_float,
    frames, target_from_structure,
)


class LiquiditySweepStrategyV2:
    """Range liquidity sweep -> reclaim -> mean-reversion model.

    Unlike the legacy implementation this strategy is intentionally NOT forced to
    follow trend direction.  It is routed only in RANGE regime and trades back
    toward the opposite side of the established range after a real sweep/reclaim.
    """

    def __init__(self):
        self.last_trace: dict = {}

    def _reject(self, reason: str, **metrics):
        self.last_trace = {"accepted": False, "reason": reason, **metrics}
        return None

    def scan(self, regime, snapshot, *, allow_watch: bool = True):
        self.last_trace = {"accepted": False, "reason": "not_evaluated"}
        if getattr(regime, "hard_block", False) or not getattr(regime, "sweep_allowed", False):
            return self._reject("regime_sweep_not_allowed")

        tf = frames(snapshot, getattr(snapshot, "candles", None))
        c5 = list(tf.get("5m") or [])
        if len(c5) < 90:
            return self._reject("insufficient_5m_bars", bars=len(c5))
        o, h, l, c, v = extract(c5)
        atr_value = float(atr(h, l, c, 14))
        current = float(c[-1])
        if atr_value <= 0 or current <= 0:
            return self._reject("invalid_market_geometry")

        n = len(c5)
        lookback = 30
        max_age = int(env_float("V2_SWEEP_MAX_AGE_BARS", 4, 2, 8))
        sweep_candidates = []
        start = max(lookback + 2, n - max_age - 2)
        for idx in range(start, n):
            prior_lows = [float(x) for x in l[idx - lookback:idx]]
            prior_highs = [float(x) for x in h[idx - lookback:idx]]
            low_level = min(prior_lows)
            high_level = max(prior_highs)
            shape = candle_shape(c5[idx])
            rv = float(relative_volume(v, idx, 20))

            long_depth = (low_level - float(l[idx])) / atr_value
            if 0.05 <= long_depth <= 1.25 and float(c[idx]) > low_level:
                reclaim = (float(c[idx]) - low_level) / atr_value
                if shape["lower_wick_ratio"] >= 0.30 and shape["close_pos"] >= 0.48 and rv >= 0.75:
                    score = 24.0 * clamp(shape["lower_wick_ratio"] / 0.65, 0.0, 1.0) + 16.0 * clamp(rv / 1.6, 0.0, 1.0) + 12.0 * clamp(reclaim / 0.45, 0.0, 1.0)
                    sweep_candidates.append((idx, Direction.LONG, low_level, high_level, long_depth, score, rv, shape))

            short_depth = (float(h[idx]) - high_level) / atr_value
            if 0.05 <= short_depth <= 1.25 and float(c[idx]) < high_level:
                reclaim = (high_level - float(c[idx])) / atr_value
                if shape["upper_wick_ratio"] >= 0.30 and shape["close_pos"] <= 0.52 and rv >= 0.75:
                    score = 24.0 * clamp(shape["upper_wick_ratio"] / 0.65, 0.0, 1.0) + 16.0 * clamp(rv / 1.6, 0.0, 1.0) + 12.0 * clamp(reclaim / 0.45, 0.0, 1.0)
                    sweep_candidates.append((idx, Direction.SHORT, high_level, low_level, short_depth, score, rv, shape))

        if not sweep_candidates:
            # Precursor watch: only follow price when it is genuinely near one edge
            # of a 30-bar range.  This keeps WATCHING selective instead of turning
            # every ranging market into a permanent tracked setup.
            low_level = min(float(x) for x in l[-lookback - 1:-1])
            high_level = max(float(x) for x in h[-lookback - 1:-1])
            dist_low = (current - low_level) / atr_value
            dist_high = (high_level - current) / atr_value
            if not allow_watch or min(dist_low, dist_high) > 0.38:
                return self._reject("no_liquidity_sweep", distance_low_atr=dist_low, distance_high_atr=dist_high)
            direction = Direction.LONG if dist_low <= dist_high else Direction.SHORT
            level = low_level if direction == Direction.LONG else high_level
            opposite = high_level if direction == Direction.LONG else low_level
            raw_stop = level - 0.65 * atr_value if direction == Direction.LONG else level + 0.65 * atr_value
            stop_info = enforce_stop_distance(current, raw_stop, atr_value, direction, min_atr=0.55, max_atr=1.80)
            if stop_info is None:
                return self._reject("watch_stop_geometry_invalid")
            stop, risk = stop_info
            target_rr = env_float("TRADE_LIQUIDITY_SWEEP_TARGET_RR", 1.30, 1.05, 1.50)
            target_info = target_from_structure(current, stop, opposite, direction, target_rr=target_rr)
            if target_info is None:
                return self._reject("watch_target_geometry_invalid")
            target, rr = target_info
            quality = clamp(58.0 + 16.0 * clamp(1.0 - min(dist_low, dist_high) / 0.38, 0.0, 1.0), 0.0, 100.0)
            candidate = StrategyCandidate(
                Strategy.LIQUIDITY_SWEEP, direction, "WATCH", quality, current, level,
                level - 0.55 * atr_value if direction == Direction.LONG else level + 0.55 * atr_value,
                stop, target, opposite, level - 0.30 * atr_value, level + 0.30 * atr_value,
                int(c5[-1].timestamp), ("range_edge_approach", "waiting_sweep"),
                {
                    "strategy_model": "liquidity_sweep_v2",
                    "structure_level": level,
                    "opposite_range_level": opposite,
                    "atr_value": atr_value,
                    "signal_entry_price": current,
                    "sl_pct": abs(current - stop) / current,
                    "execution_rr": rr,
                    "structural_target_price": opposite,
                    "target_rr_cap": target_rr,
                },
            )
            self.last_trace = {"accepted": True, "reason": "watch_range_edge", "stage": "WATCH", "side": direction.value, "quality": quality, "level": level}
            return candidate

        sweep_idx, direction, level, opposite, depth, sweep_score, sweep_rvol, sweep_shape = max(
            sweep_candidates, key=lambda item: (item[0], item[5])
        )
        age = n - 1 - sweep_idx
        sweep_extreme = float(l[sweep_idx] if direction == Direction.LONG else h[sweep_idx])

        # Confirm that price is actually leaving the swept edge, not merely
        # printing a wick and sitting on the invalidation zone.
        confirm_idx = None
        for idx in range(sweep_idx, n):
            shape = candle_shape(c5[idx])
            if direction == Direction.LONG:
                confirmed = float(c[idx]) > level + 0.05 * atr_value and float(c[idx]) >= float(o[idx]) and shape["close_pos"] >= 0.55 and shape["body_ratio"] >= 0.20
            else:
                confirmed = float(c[idx]) < level - 0.05 * atr_value and float(c[idx]) <= float(o[idx]) and shape["close_pos"] <= 0.45 and shape["body_ratio"] >= 0.20
            if confirmed:
                confirm_idx = idx
                break

        if confirm_idx is None:
            if not allow_watch:
                return self._reject("sweep_without_confirmation")
            raw_stop = sweep_extreme - 0.18 * atr_value if direction == Direction.LONG else sweep_extreme + 0.18 * atr_value
            stop_info = enforce_stop_distance(current, raw_stop, atr_value, direction, min_atr=0.55, max_atr=1.80)
            if stop_info is None:
                return self._reject("watch_stop_geometry_invalid")
            stop, risk = stop_info
            target_rr = env_float("TRADE_LIQUIDITY_SWEEP_TARGET_RR", 1.30, 1.05, 1.50)
            target_info = target_from_structure(current, stop, opposite, direction, target_rr=target_rr)
            if target_info is None:
                return self._reject("watch_target_geometry_invalid")
            target, rr = target_info
            quality = clamp(60.0 + sweep_score * 0.45 - age * 2.0, 0.0, 100.0)
            candidate = StrategyCandidate(
                Strategy.LIQUIDITY_SWEEP, direction, "WATCH", quality, current, level,
                sweep_extreme - 0.12 * atr_value if direction == Direction.LONG else sweep_extreme + 0.12 * atr_value,
                stop, target, opposite, level - 0.32 * atr_value, level + 0.32 * atr_value,
                int(c5[sweep_idx].timestamp), ("liquidity_swept", "level_reclaimed", "waiting_reversal_confirmation"),
                {
                    "strategy_model": "liquidity_sweep_v2",
                    "structure_level": level,
                    "sweep_idx": sweep_idx,
                    "sweep_depth_atr": depth,
                    "sweep_rvol": sweep_rvol,
                    "atr_value": atr_value,
                    "signal_entry_price": current,
                    "sl_pct": abs(current - stop) / current,
                    "execution_rr": rr,
                    "structural_target_price": opposite,
                    "target_rr_cap": target_rr,
                },
            )
            self.last_trace = {"accepted": True, "reason": "watch_sweep_confirmation", "stage": "WATCH", "quality": quality, "side": direction.value, "level": level}
            return candidate

        confirm_age = n - 1 - confirm_idx
        if confirm_age > 2:
            return self._reject("sweep_confirmation_stale", age=confirm_age)
        if direction == Direction.LONG and current < level - 0.15 * atr_value:
            return self._reject("sweep_reclaim_lost")
        if direction == Direction.SHORT and current > level + 0.15 * atr_value:
            return self._reject("sweep_reclaim_lost")

        raw_stop = sweep_extreme - 0.18 * atr_value if direction == Direction.LONG else sweep_extreme + 0.18 * atr_value
        stop_info = enforce_stop_distance(current, raw_stop, atr_value, direction, min_atr=0.55, max_atr=1.80)
        if stop_info is None:
            return self._reject("stop_geometry_invalid")
        stop, risk = stop_info
        target_rr = env_float("TRADE_LIQUIDITY_SWEEP_TARGET_RR", 1.30, 1.05, 1.50)
        target_info = target_from_structure(current, stop, opposite, direction, target_rr=target_rr)
        if target_info is None:
            return self._reject("target_geometry_invalid")
        target, rr = target_info

        confirm_shape = candle_shape(c5[confirm_idx])
        range_width_atr = abs(opposite - level) / atr_value
        quality = clamp(
            42.0
            + sweep_score * 0.50
            + 10.0 * clamp(range_width_atr / 5.0, 0.0, 1.0)
            + 8.0 * confirm_shape["body_ratio"]
            + 6.0 * clamp(1.0 - confirm_age / 3.0, 0.0, 1.0),
            0.0, 100.0,
        )
        min_score = env_float("V2_SWEEP_MIN_SCORE", 68.0, 50.0, 90.0)
        if quality < min_score:
            return self._reject("quality_below_threshold", quality=quality, minimum=min_score)

        trigger = float(c5[confirm_idx].high if direction == Direction.LONG else c5[confirm_idx].low)
        candidate = StrategyCandidate(
            Strategy.LIQUIDITY_SWEEP, direction, "READY", quality, current, trigger,
            sweep_extreme - 0.12 * atr_value if direction == Direction.LONG else sweep_extreme + 0.12 * atr_value,
            stop, target, opposite, level - 0.34 * atr_value, level + 0.34 * atr_value,
            int(c5[sweep_idx].timestamp), ("liquidity_swept", "level_reclaimed", "mean_reversion_confirmed"),
            {
                "strategy_model": "liquidity_sweep_v2",
                "structure_level": level,
                "opposite_range_level": opposite,
                "sweep_idx": sweep_idx,
                "sweep_age_bars": age,
                "sweep_depth_atr": depth,
                "sweep_rvol": sweep_rvol,
                "sweep_wick_ratio": sweep_shape["lower_wick_ratio"] if direction == Direction.LONG else sweep_shape["upper_wick_ratio"],
                "confirmation_idx": confirm_idx,
                "confirmation_body_ratio": confirm_shape["body_ratio"],
                "atr_value": atr_value,
                "signal_entry_price": current,
                "sl_pct": abs(current - stop) / current,
                "tp_pct": abs(target - current) / current,
                "execution_rr": rr,
                "structural_rr_estimate": abs(opposite - current) / risk,
                "structural_target_price": opposite,
                "target_rr_cap": target_rr,
            },
        )
        self.last_trace = {
            "accepted": True, "reason": "setup_ready", "stage": "READY", "quality": quality,
            "side": direction.value, "level": level, "entry": current, "stop": stop, "target": target,
            "execution_rr": rr, "sweep_age_bars": age,
        }
        return candidate
