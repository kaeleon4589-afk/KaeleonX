from __future__ import annotations

import math
import time
from typing import Any

from app.models.enums import Direction, Strategy
from app.models.trading import ArmedSetup, TradeIntent
from app.position.protection import (
    break_even_activation_ratio,
    front_run_target,
    profit_lock_activation_ratio,
    profit_lock_capture_ratio,
)
from app.strategy import breakout_retest as breakout
from app.strategy import liquidity_sweep as sweep
from app.strategy.source_math import atr, candle_quality, clamp, ema, extract, relative_volume

# v6 deliberately separates setup discovery from execution confirmation.
# The setup is discovered from closed 5m structure, then a closed 1m candle plus
# the live executable quote must confirm before an order can be submitted.
BREAKOUT_ARM_MIN_SCORE = 68.0
SWEEP_ARM_MIN_SCORE = 68.0
ARM_MIN_RR = 1.10
ARM_MIN_STOP_ATR = 0.28
ARM_MAX_STOP_ATR = 1.80
BREAKOUT_ENTRY_MAX_EXTENSION_ATR = 0.62
SWEEP_ENTRY_MAX_EXTENSION_ATR = 0.90
MICRO_TRIGGER_MIN_BODY_RATIO = 0.22
MICRO_TRIGGER_MIN_RVOL = 0.70
MICRO_TRIGGER_CLOSE_LONG = 0.60
MICRO_TRIGGER_CLOSE_SHORT = 0.40


def _now_ms() -> int:
    return int(time.time() * 1000)


def _shape(candle) -> tuple[float, float]:
    span = max(float(candle.high) - float(candle.low), 1e-12)
    body = abs(float(candle.close) - float(candle.open)) / span
    pos = clamp((float(candle.close) - float(candle.low)) / span, 0.0, 1.0)
    return body, pos


def _micro_confirmation(setup: ArmedSetup, snapshot) -> tuple[bool, str, dict]:
    frames = getattr(snapshot, "timeframes", {}) or {}
    candles_1m = list(frames.get("1m", []) or [])
    executable = getattr(snapshot, "ask" if setup.direction == Direction.LONG else "bid", None)
    try:
        executable = float(executable)
    except (TypeError, ValueError):
        return False, "quote_unavailable", {}
    if not math.isfinite(executable) or executable <= 0:
        return False, "quote_unavailable", {}

    if setup.direction == Direction.LONG:
        if executable <= setup.invalidation_price:
            return False, "setup_invalidated", {"price": executable}
        if executable > setup.entry_zone_high:
            return False, "setup_chased", {"price": executable, "entry_zone_high": setup.entry_zone_high}
        crossed = executable >= setup.trigger_price
    else:
        if executable >= setup.invalidation_price:
            return False, "setup_invalidated", {"price": executable}
        if executable < setup.entry_zone_low:
            return False, "setup_chased", {"price": executable, "entry_zone_low": setup.entry_zone_low}
        crossed = executable <= setup.trigger_price
    if not crossed:
        return False, "waiting_trigger", {"price": executable, "trigger_price": setup.trigger_price}

    # We intentionally require a CLOSED 1m candle when available.  This prevents
    # a single quote spike from triggering the setup.  If the provider omitted 1m
    # data, keep waiting instead of degrading silently to the old late-entry path.
    if not candles_1m:
        return False, "waiting_1m_confirmation", {"price": executable}
    last = candles_1m[-1]
    # Never let a candle that closed before the setup was armed act as the
    # confirmation. Otherwise the next 2s scanner pass could reuse stale 1m
    # evidence and recreate the same late-entry problem v6 is meant to remove.
    micro_close_ms = int(getattr(last, "timestamp", 0) or 0) + 60_000
    if micro_close_ms <= int(setup.armed_at_ms):
        return False, "waiting_fresh_1m_confirmation", {
            "price": executable, "micro_close_ms": micro_close_ms,
            "armed_at_ms": int(setup.armed_at_ms),
        }
    if setup.direction == Direction.LONG and float(last.low) <= setup.invalidation_price:
        return False, "setup_invalidated", {"price": executable, "1m_low": float(last.low)}
    if setup.direction == Direction.SHORT and float(last.high) >= setup.invalidation_price:
        return False, "setup_invalidated", {"price": executable, "1m_high": float(last.high)}
    body, close_pos = _shape(last)
    volumes = [float(c.volume) for c in candles_1m]
    rvol = relative_volume(volumes, len(volumes) - 1, 20)
    close = float(last.close)
    opened = float(last.open)
    if setup.direction == Direction.LONG:
        ok = (
            close > opened
            and close >= setup.trigger_price
            and body >= MICRO_TRIGGER_MIN_BODY_RATIO
            and close_pos >= MICRO_TRIGGER_CLOSE_LONG
            and rvol >= MICRO_TRIGGER_MIN_RVOL
        )
    else:
        ok = (
            close < opened
            and close <= setup.trigger_price
            and body >= MICRO_TRIGGER_MIN_BODY_RATIO
            and close_pos <= MICRO_TRIGGER_CLOSE_SHORT
            and rvol >= MICRO_TRIGGER_MIN_RVOL
        )
    if not ok:
        return False, "waiting_1m_confirmation", {
            "price": executable,
            "1m_close": close,
            "1m_body_ratio": round(body, 4),
            "1m_close_pos": round(close_pos, 4),
            "1m_rvol": round(rvol, 4),
        }
    return True, "triggered", {
        "price": executable,
        "1m_close": close,
        "1m_body_ratio": round(body, 4),
        "1m_close_pos": round(close_pos, 4),
        "1m_rvol": round(rvol, 4),
    }


def _management_metadata(entry: float, stop: float, target: float, structural_target: float,
                         target_ratio: float, atr_value: float) -> dict:
    sl_pct = abs(entry - stop) / max(entry, 1e-12)
    tp_pct = abs(target - entry) / max(entry, 1e-12)
    atr_pct = atr_value / max(entry, 1e-12)
    return {
        "atr_value": atr_value,
        "atr_pct": atr_pct,
        "sl_pct": sl_pct,
        "tp_pct": tp_pct,
        "structural_tp_pct": abs(structural_target - entry) / max(entry, 1e-12),
        "structural_target_price": structural_target,
        "target_front_run_ratio": target_ratio,
        "execution_rr": tp_pct / max(sl_pct, 1e-12),
        "partial_tp_enabled": False,
        "tp2_price": target,
        "break_even_activation_ratio": break_even_activation_ratio(),
        "profit_lock_activation_ratio": profit_lock_activation_ratio(),
        "profit_lock_capture_ratio": profit_lock_capture_ratio(),
        "break_even_activation_pct": clamp(max(sl_pct * 0.45, tp_pct * 0.38), 0.0025, min(tp_pct * 0.58, 0.0048)),
        "break_even_offset_pct": clamp(max(atr_pct * 0.06, 0.0005), 0.0005, 0.0010),
    }


class ArmedEntryEngine:
    def __init__(self, ttl_seconds: float = 600.0):
        self.ttl_seconds = max(60.0, float(ttl_seconds))
        self.last_trace: dict[str, Any] = {}
        self.branch_trace: dict[str, dict[str, Any]] = {}

    def _expiry(self, snapshot) -> tuple[int, int]:
        armed_at = _now_ms()
        return armed_at, armed_at + int(self.ttl_seconds * 1000)

    @staticmethod
    def _active_regime(regime_metadata: dict | None) -> str:
        return str((regime_metadata or {}).get("active") or "UNKNOWN").upper()

    def _discover_breakout(self, regime, snapshot, symbol: str, timeframe: str,
                           regime_metadata: dict | None) -> ArmedSetup | None:
        frames = getattr(snapshot, "timeframes", {}) or {}
        c5 = list(frames.get("5m", getattr(snapshot, "candles", [])) or [])
        c15 = list(frames.get("15m", []) or [])
        c1h = list(frames.get("1h", []) or [])
        ok, _ = candle_quality(c5, breakout.MIN_CANDLES_REQUIRED, breakout.MIN_NONZERO_VOLUME_RATIO)
        if not ok or len(c15) < 200 or len(c1h) < 200:
            self.branch_trace["breakout"] = {"accepted": False, "reason": "insufficient_or_bad_mtf_data"}
            return None
        tf5, tf15, tf1h = breakout._tf_values(c5), breakout._tf_values(c15), breakout._tf_values(c1h)
        close5 = float(tf5["c"][-1])
        atr5 = float(tf5["atr"])
        if atr5 <= 0:
            self.branch_trace["breakout"] = {"accepted": False, "reason": "invalid_atr"}
            return None
        atr_pct = atr5 / max(close5, 1e-12)
        if not breakout.ATR_PCT_MIN <= atr_pct <= breakout.ATR_PCT_MAX:
            self.branch_trace["breakout"] = {"accepted": False, "reason": "atr_out_of_range", "atr_pct": atr_pct}
            return None

        bias1h, diag1h = breakout._bias(tf1h, adx_min=breakout.H1_ADX_MIN)
        bias15, diag15 = breakout._bias(tf15, adx_min=breakout.M15_ADX_MIN)
        biases = [x for x in (bias1h, bias15) if x != "none"]
        if not biases or (len(biases) == 2 and biases[0] != biases[1]):
            self.branch_trace["breakout"] = {
                "accepted": False, "reason": "mtf_bias_missing_or_conflict",
                "bias_1h": bias1h, "bias_15m": bias15,
            }
            return None
        direction_name = biases[0]
        exhausted, exhaustion_diag = breakout._htf_exhaustion(direction_name, tf1h, tf15)
        if exhausted:
            self.branch_trace["breakout"] = {"accepted": False, "reason": "higher_timeframe_move_exhausted", **exhaustion_diag}
            return None

        o, h, l, c, v = tf5["o"], tf5["h"], tf5["l"], tf5["c"], tf5["v"]
        i = len(c) - 1
        best = None
        for retest_count in range(1, breakout.RETEST_MAX_BARS_AFTER_BREAKOUT + 1):
            breakout_idx = i - retest_count
            if breakout_idx <= breakout.STRUCTURE_LOOKBACK_BARS:
                continue
            start = breakout_idx - breakout.STRUCTURE_LOOKBACK_BARS
            structural_level = max(h[start:breakout_idx]) if direction_name == "long" else min(l[start:breakout_idx])
            body, close_pos = breakout._candle_shape(o, h, l, c, breakout_idx)
            rvol = relative_volume(v, breakout_idx)
            extension = abs(float(c[breakout_idx]) - float(structural_level)) / atr5
            if direction_name == "long":
                breakout_ok = (
                    float(c[breakout_idx]) > float(o[breakout_idx])
                    and float(c[breakout_idx]) >= float(structural_level) + atr5 * breakout.BREAKOUT_CLOSE_BUFFER_ATR
                    and body >= breakout.BREAKOUT_MIN_BODY_RATIO
                    and close_pos >= breakout.BREAKOUT_CLOSE_POS_LONG_MIN
                    and rvol >= breakout.BREAKOUT_MIN_RVOL
                    and extension <= breakout.BREAKOUT_MAX_EXTENSION_ATR
                )
            else:
                breakout_ok = (
                    float(c[breakout_idx]) < float(o[breakout_idx])
                    and float(c[breakout_idx]) <= float(structural_level) - atr5 * breakout.BREAKOUT_CLOSE_BUFFER_ATR
                    and body >= breakout.BREAKOUT_MIN_BODY_RATIO
                    and close_pos <= breakout.BREAKOUT_CLOSE_POS_SHORT_MAX
                    and rvol >= breakout.BREAKOUT_MIN_RVOL
                    and extension <= breakout.BREAKOUT_MAX_EXTENSION_ATR
                )
            if not breakout_ok:
                continue

            retest_indices = list(range(breakout_idx + 1, i + 1))
            if not retest_indices:
                continue
            if direction_name == "long":
                extreme = min(float(l[j]) for j in retest_indices)
                touch = extreme <= structural_level + atr5 * breakout.RETEST_TOUCH_TOL_ATR
                no_deep = extreme >= structural_level - atr5 * breakout.RETEST_MAX_PENETRATION_ATR
                closes_valid = all(float(c[j]) >= structural_level - atr5 * breakout.RETEST_CLOSE_INVALIDATION_ATR for j in retest_indices)
                last_near = abs(float(c[i]) - structural_level) <= atr5 * max(breakout.RETEST_MAX_CLOSE_DISTANCE_ATR, 0.34)
                pullback = max(0.0, float(c[breakout_idx]) - extreme) / atr5
                trigger = max(structural_level + atr5 * 0.04, float(c[i]) + atr5 * 0.02)
                stop = extreme - atr5 * breakout.MTF_SL_BUFFER_ATR
                entry_zone_low = trigger
                entry_zone_high = structural_level + atr5 * BREAKOUT_ENTRY_MAX_EXTENSION_ATR
                direction = Direction.LONG
            else:
                extreme = max(float(h[j]) for j in retest_indices)
                touch = extreme >= structural_level - atr5 * breakout.RETEST_TOUCH_TOL_ATR
                no_deep = extreme <= structural_level + atr5 * breakout.RETEST_MAX_PENETRATION_ATR
                closes_valid = all(float(c[j]) <= structural_level + atr5 * breakout.RETEST_CLOSE_INVALIDATION_ATR for j in retest_indices)
                last_near = abs(float(c[i]) - structural_level) <= atr5 * max(breakout.RETEST_MAX_CLOSE_DISTANCE_ATR, 0.34)
                pullback = max(0.0, extreme - float(c[breakout_idx])) / atr5
                trigger = min(structural_level - atr5 * 0.04, float(c[i]) - atr5 * 0.02)
                stop = extreme + atr5 * breakout.MTF_SL_BUFFER_ATR
                entry_zone_low = structural_level - atr5 * BREAKOUT_ENTRY_MAX_EXTENSION_ATR
                entry_zone_high = trigger
                direction = Direction.SHORT
            if not (touch and no_deep and closes_valid and last_near) or pullback < 0.12:
                continue
            stop_atr = abs(trigger - stop) / atr5
            if not ARM_MIN_STOP_ATR <= stop_atr <= ARM_MAX_STOP_ATR:
                continue

            structural_target = breakout._structure_target(direction, trigger, h, l, tf15["h"], tf15["l"])
            target, target_ratio = front_run_target(trigger, structural_target, direction)
            rr = abs(target - trigger) / max(abs(trigger - stop), 1e-12)
            if target <= 0 or rr < ARM_MIN_RR:
                continue
            path = abs(structural_target - structural_level)
            consumed = abs(trigger - structural_level) / max(path, 1e-12)
            if path >= atr5 * 0.45 and consumed > 0.58:
                continue

            htf_q = clamp(((float(diag1h.get("adx", 0)) - 12) + (float(diag15.get("adx", 0)) - 10)) / 30, 0, 1)
            breakout_q = clamp((rvol - 0.8) / 1.0, 0, 1)
            rr_q = clamp((rr - ARM_MIN_RR) / 1.6, 0, 1)
            fresh_q = clamp(1.0 - (retest_count - 1) / 3.0, 0, 1)
            score = round(58 + 42 * clamp(0.34 * htf_q + 0.22 * breakout_q + 0.26 * rr_q + 0.18 * fresh_q, 0, 1), 2)
            if score < BREAKOUT_ARM_MIN_SCORE:
                continue
            armed_at, expires_at = self._expiry(snapshot)
            candidate = ArmedSetup(
                setup_id=f"{symbol}:BR:{int(getattr(c5[breakout_idx], 'timestamp', armed_at))}:{direction.value}",
                symbol=symbol,
                strategy=Strategy.BREAKOUT_RETEST,
                direction=direction,
                armed_at_ms=armed_at,
                expires_at_ms=expires_at,
                trigger_price=float(trigger),
                invalidation_price=float(stop),
                stop_price=float(stop),
                target_price=float(target),
                entry_zone_low=float(min(entry_zone_low, entry_zone_high)),
                entry_zone_high=float(max(entry_zone_low, entry_zone_high)),
                quality=score,
                risk_multiplier=max(float(getattr(regime, "risk_multiplier", 1.0) or 1.0), 0.65),
                timeframe=timeframe,
                reasons=("breakout_confirmed_5m", "retest_complete", "awaiting_micro_confirmation"),
                metadata={
                    "strategy_model": "armed_breakout_retest_v6",
                    "atr_value": atr5,
                    "atr_pct": atr_pct,
                    "structural_level": float(structural_level),
                    "retest_extreme": float(extreme),
                    "breakout_rvol": float(rvol),
                    "breakout_age_bars": int(retest_count),
                    "structural_target_price": float(structural_target),
                    "target_front_run_ratio": float(target_ratio),
                    "structural_rr_estimate": float(rr),
                    "stop_atr_5m": float(stop_atr),
                    "regime": self._active_regime(regime_metadata),
                    **exhaustion_diag,
                },
            )
            if best is None or candidate.quality > best.quality:
                best = candidate
        self.branch_trace["breakout"] = (
            {"accepted": True, "reason": "setup_armable", "quality": best.quality, "direction": best.direction.value}
            if best is not None else
            {"accepted": False, "reason": "no_fresh_breakout_retest_arm"}
        )
        return best

    def _discover_sweep(self, regime, snapshot, symbol: str, timeframe: str,
                        regime_metadata: dict | None) -> ArmedSetup | None:
        frames = getattr(snapshot, "timeframes", {}) or {}
        c5 = list(frames.get("5m", getattr(snapshot, "candles", [])) or [])
        ok, _ = candle_quality(c5, sweep.MIN_CANDLES_REQUIRED, sweep.MIN_NONZERO_VOLUME_RATIO)
        if not ok:
            self.branch_trace["sweep"] = {"accepted": False, "reason": "bad_5m_candle_quality"}
            return None
        o, h, l, c, v = extract(c5)
        close5 = float(c[-1])
        atr5 = atr(h, l, c, 14)
        if atr5 <= 0:
            self.branch_trace["sweep"] = {"accepted": False, "reason": "invalid_atr"}
            return None
        atr_pct = atr5 / max(close5, 1e-12)
        if not sweep.ATR_PCT_MIN <= atr_pct <= sweep.ATR_PCT_MAX:
            self.branch_trace["sweep"] = {"accepted": False, "reason": "atr_out_of_range", "atr_pct": atr_pct}
            return None
        ema20, ema50 = ema(c, sweep.EMA_FAST), ema(c, sweep.EMA_MID)
        i = len(c) - 1
        best = None
        start_idx = max(sweep.SWEEP_LOOKBACK + 2, i - 3)
        for sweep_idx in range(start_idx, i + 1):
            left = max(0, sweep_idx - sweep.SWEEP_LOOKBACK)
            if sweep_idx - left < 12:
                continue
            for direction_name in ("long", "short"):
                if direction_name == "long":
                    level = min(l[left:sweep_idx])
                    extreme = float(l[sweep_idx])
                    depth = (float(level) - extreme) / atr5
                    wick = sweep._lower_wick(o[sweep_idx], h[sweep_idx], l[sweep_idx], c[sweep_idx])
                    sweep_close = sweep._close_pos(h[sweep_idx], l[sweep_idx], c[sweep_idx])
                    recovered = float(c[sweep_idx]) >= float(level) - atr5 * sweep.SWEEP_RECOVER_TOL_ATR and sweep_close >= 0.46
                    direction = Direction.LONG
                else:
                    level = max(h[left:sweep_idx])
                    extreme = float(h[sweep_idx])
                    depth = (extreme - float(level)) / atr5
                    wick = sweep._upper_wick(o[sweep_idx], h[sweep_idx], l[sweep_idx], c[sweep_idx])
                    sweep_close = sweep._close_pos(h[sweep_idx], l[sweep_idx], c[sweep_idx])
                    recovered = float(c[sweep_idx]) <= float(level) + atr5 * sweep.SWEEP_RECOVER_TOL_ATR and sweep_close <= 0.54
                    direction = Direction.SHORT
                rvol = relative_volume(v, sweep_idx, 24)
                if depth < sweep.SWEEP_MIN_DEPTH_ATR or wick < sweep.SWEEP_MIN_WICK_RATIO or rvol < sweep.SWEEP_MIN_RVOL or not recovered:
                    continue
                if sweep_idx < i:
                    if direction == Direction.LONG and min(l[sweep_idx + 1:i + 1]) < extreme - atr5 * sweep.RETEST_INVALIDATION_ATR:
                        continue
                    if direction == Direction.SHORT and max(h[sweep_idx + 1:i + 1]) > extreme + atr5 * sweep.RETEST_INVALIDATION_ATR:
                        continue
                if direction == Direction.LONG:
                    trigger = max(float(level) + atr5 * 0.04, float(c[sweep_idx]) + atr5 * 0.03)
                    stop = extreme - atr5 * sweep.SL_BUFFER_ATR
                    entry_zone_low, entry_zone_high = trigger, float(level) + atr5 * SWEEP_ENTRY_MAX_EXTENSION_ATR
                    target_level = max(h[max(0, sweep_idx - sweep.TARGET_LOOKBACK):sweep_idx])
                else:
                    trigger = min(float(level) - atr5 * 0.04, float(c[sweep_idx]) - atr5 * 0.03)
                    stop = extreme + atr5 * sweep.SL_BUFFER_ATR
                    entry_zone_low, entry_zone_high = float(level) - atr5 * SWEEP_ENTRY_MAX_EXTENSION_ATR, trigger
                    target_level = min(l[max(0, sweep_idx - sweep.TARGET_LOOKBACK):sweep_idx])
                stop_atr = abs(trigger - stop) / atr5
                if not ARM_MIN_STOP_ATR <= stop_atr <= ARM_MAX_STOP_ATR:
                    continue
                target, target_ratio = front_run_target(trigger, target_level, direction)
                rr = abs(target - trigger) / max(abs(trigger - stop), 1e-12)
                if target <= 0 or rr < ARM_MIN_RR:
                    continue
                age = i - sweep_idx
                depth_q = clamp((depth - sweep.SWEEP_MIN_DEPTH_ATR) / 0.65, 0, 1)
                wick_q = clamp((wick - sweep.SWEEP_MIN_WICK_RATIO) / 0.45, 0, 1)
                rv_q = clamp((rvol - 0.65) / 1.0, 0, 1)
                rr_q = clamp((rr - ARM_MIN_RR) / 1.6, 0, 1)
                fresh_q = clamp(1.0 - age / 4.0, 0, 1)
                score = round(58 + 42 * clamp(0.25 * depth_q + 0.20 * wick_q + 0.16 * rv_q + 0.25 * rr_q + 0.14 * fresh_q, 0, 1), 2)
                if score < SWEEP_ARM_MIN_SCORE:
                    continue
                armed_at, expires_at = self._expiry(snapshot)
                candidate = ArmedSetup(
                    setup_id=f"{symbol}:LS:{int(getattr(c5[sweep_idx], 'timestamp', armed_at))}:{direction.value}",
                    symbol=symbol,
                    strategy=Strategy.LIQUIDITY_SWEEP,
                    direction=direction,
                    armed_at_ms=armed_at,
                    expires_at_ms=expires_at,
                    trigger_price=float(trigger),
                    invalidation_price=float(stop),
                    stop_price=float(stop),
                    target_price=float(target),
                    entry_zone_low=float(min(entry_zone_low, entry_zone_high)),
                    entry_zone_high=float(max(entry_zone_low, entry_zone_high)),
                    quality=score,
                    risk_multiplier=max(float(getattr(regime, "risk_multiplier", 1.0) or 1.0), 0.65),
                    timeframe=timeframe,
                    reasons=("liquidity_sweep_5m", "level_recovered", "awaiting_micro_confirmation"),
                    metadata={
                        "strategy_model": "armed_liquidity_sweep_v6",
                        "atr_value": atr5,
                        "atr_pct": atr_pct,
                        "sweep_level": float(level),
                        "sweep_extreme": float(extreme),
                        "sweep_depth_atr": float(depth),
                        "sweep_wick_ratio": float(wick),
                        "sweep_rvol": float(rvol),
                        "bars_since_sweep": int(age),
                        "structural_target_price": float(target_level),
                        "target_front_run_ratio": float(target_ratio),
                        "structural_rr_estimate": float(rr),
                        "stop_atr_5m": float(stop_atr),
                        "regime": self._active_regime(regime_metadata),
                    },
                )
                if best is None or candidate.quality > best.quality:
                    best = candidate
        self.branch_trace["sweep"] = (
            {"accepted": True, "reason": "setup_armable", "quality": best.quality, "direction": best.direction.value}
            if best is not None else
            {"accepted": False, "reason": "no_liquidity_sweep_to_arm"}
        )
        return best

    def discover(self, regime, snapshot, symbol: str, timeframe: str,
                 regime_metadata: dict | None = None) -> ArmedSetup | None:
        self.branch_trace = {}
        self.last_trace = {"accepted": False, "reason": "no_armable_setup", "branches": {}}
        if getattr(regime, "hard_block", False) and self._active_regime(regime_metadata) == "UNKNOWN":
            self.last_trace = {"accepted": False, "reason": "regime_unknown_hard_block"}
            return None
        active = self._active_regime(regime_metadata)
        scores = (regime_metadata or {}).get("scores") or {}
        candidates: list[ArmedSetup] = []
        # Regime is context, not a duplicate absolute gate. Breakouts are primarily
        # trend setups. Sweeps are valid in volatile/range markets and can also be
        # probed in trend when volatility evidence is strong.
        if active == "TREND_CONTINUATION" or getattr(regime, "breakout_allowed", False):
            item = self._discover_breakout(regime, snapshot, symbol, timeframe, regime_metadata)
            if item:
                candidates.append(item)
        volatile_score = float(scores.get("VOLATILE_SWEEP") or 0.0)
        if active in {"VOLATILE_SWEEP", "RANGE"} or getattr(regime, "sweep_allowed", False) or volatile_score >= 2.0:
            item = self._discover_sweep(regime, snapshot, symbol, timeframe, regime_metadata)
            if item:
                candidates.append(item)
        if not candidates:
            primary = "no_armable_setup"
            if active == "TREND_CONTINUATION" and self.branch_trace.get("breakout"):
                primary = str(self.branch_trace["breakout"].get("reason") or primary)
            elif active in {"VOLATILE_SWEEP", "RANGE"} and self.branch_trace.get("sweep"):
                primary = str(self.branch_trace["sweep"].get("reason") or primary)
            elif self.branch_trace:
                primary = str(next(iter(self.branch_trace.values())).get("reason") or primary)
            self.last_trace = {"accepted": False, "reason": primary, "branches": dict(self.branch_trace)}
            return None
        selected = max(candidates, key=lambda item: (item.quality, item.strategy.value))
        self.last_trace = {
            "accepted": True,
            "reason": "setup_armed",
            "strategy": selected.strategy.value,
            "direction": selected.direction.value,
            "quality": selected.quality,
            "trigger_price": selected.trigger_price,
            "expires_at_ms": selected.expires_at_ms,
            "branches": dict(self.branch_trace),
        }
        return selected

    def trigger(self, setup: ArmedSetup, snapshot, decision_id: str) -> tuple[str, TradeIntent | None, dict]:
        now_ms = _now_ms()
        if now_ms >= int(setup.expires_at_ms):
            return "cancelled", None, {"reason": "setup_expired"}
        ok, reason, diag = _micro_confirmation(setup, snapshot)
        if not ok:
            if reason in {"setup_invalidated", "setup_chased"}:
                return "cancelled", None, {"reason": reason, **diag}
            return "pending", None, {"reason": reason, **diag}
        executable = float(diag["price"])
        target = float(setup.target_price)
        stop = float(setup.stop_price)
        geometry = (
            setup.direction == Direction.LONG and stop < executable < target
        ) or (
            setup.direction == Direction.SHORT and target < executable < stop
        )
        if not geometry:
            return "cancelled", None, {"reason": "trigger_invalid_geometry", **diag}
        rr = abs(target - executable) / max(abs(executable - stop), 1e-12)
        if rr < ARM_MIN_RR:
            return "cancelled", None, {"reason": "trigger_rr_too_low", "execution_rr": rr, **diag}
        structural_target = float(setup.metadata.get("structural_target_price") or target)
        target_ratio = float(setup.metadata.get("target_front_run_ratio") or 1.0)
        management = _management_metadata(executable, stop, target, structural_target, target_ratio,
                                          float(setup.metadata.get("atr_value") or 0.0))
        metadata = {
            **setup.metadata,
            **management,
            "armed_setup_id": setup.setup_id,
            "entry_model": "armed_v6",
            "armed_at_ms": setup.armed_at_ms,
            "triggered_at_ms": now_ms,
            "trigger_price": setup.trigger_price,
            "entry_zone_low": setup.entry_zone_low,
            "entry_zone_high": setup.entry_zone_high,
            "micro_confirmation": diag,
        }
        intent = TradeIntent(
            decision_id=decision_id,
            symbol=setup.symbol,
            strategy=setup.strategy,
            direction=setup.direction,
            entry_price=executable,
            stop_price=stop,
            target_price=target,
            quality=min(100.0, float(setup.quality) + 5.0),
            risk_multiplier=setup.risk_multiplier,
            timeframe=setup.timeframe,
            reasons=tuple(setup.reasons) + ("micro_confirmation_1m",),
            metadata=metadata,
        )
        return "triggered", intent, {"reason": "micro_confirmation_1m", "execution_rr": rr, **diag}
