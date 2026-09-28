from __future__ import annotations

import math
import time
from typing import Any

from app.models.enums import Direction, Strategy
from app.models.trading import ArmedSetup, TradeIntent
from app.position.protection import (
    break_even_activation_ratio,
    breakout_retest_target_rr,
    cap_target_by_rr,
    front_run_target,
    liquidity_sweep_target_rr,
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
# Slightly wider freshness window for breakout/retest discovery. The structural,
# MTF, exhaustion, retest, RR and 1m confirmation gates remain unchanged.
BREAKOUT_ARM_MAX_RETEST_BARS = max(breakout.RETEST_MAX_BARS_AFTER_BREAKOUT, 4)
SWEEP_ENTRY_MAX_EXTENSION_ATR = 0.90
MICRO_TRIGGER_MIN_BODY_RATIO = 0.22
MICRO_TRIGGER_MIN_RVOL = 0.70
MICRO_TRIGGER_CLOSE_LONG = 0.60
MICRO_TRIGGER_CLOSE_SHORT = 0.40
# Fast confirmation is intentionally stricter than the normal post-arm 1m path.
# It may reuse the most recent CLOSED 1m candle only when that candle closed
# immediately before arming and already showed strong directional acceptance.
FAST_CONFIRM_MIN_SETUP_QUALITY = 78.0
FAST_CONFIRM_MIN_BODY_RATIO = 0.45
FAST_CONFIRM_MIN_RVOL = 0.90
FAST_CONFIRM_CLOSE_LONG = 0.72
FAST_CONFIRM_CLOSE_SHORT = 0.28
CHASE_EDGE_MAX_ATR = 0.02


def _now_ms() -> int:
    return int(time.time() * 1000)


def _shape(candle) -> tuple[float, float]:
    span = max(float(candle.high) - float(candle.low), 1e-12)
    body = abs(float(candle.close) - float(candle.open)) / span
    pos = clamp((float(candle.close) - float(candle.low)) / span, 0.0, 1.0)
    return body, pos



def _book_tick_size(snapshot) -> float:
    """Best-effort tick estimate from the visible order book.

    CoinW depth does not expose tick size on this lightweight path.  We infer the
    smallest positive step from the top levels and use it only as a *tiny* edge
    tolerance around the anti-chase boundary.  It never replaces the ATR chase
    limit and is hard-capped below.
    """
    prices: list[float] = []
    for side_name in ("bids", "asks"):
        for level in list(getattr(snapshot, side_name, []) or [])[:12]:
            try:
                price = float(level[0] if isinstance(level, (list, tuple)) else level.get("price"))
            except (TypeError, ValueError, AttributeError, IndexError):
                continue
            if math.isfinite(price) and price > 0:
                prices.append(price)
    uniq = sorted(set(prices))
    diffs = [b - a for a, b in zip(uniq, uniq[1:]) if b > a]
    return min(diffs) if diffs else 0.0


def _closed_1m_quality(setup: ArmedSetup, candle, candles_1m, *, close_buffer: float, chase_limit: float,
                       strict: bool = False) -> tuple[bool, dict]:
    body, close_pos = _shape(candle)
    volumes = [float(c.volume) for c in candles_1m]
    rvol = relative_volume(volumes, len(volumes) - 1, 20)
    close = float(candle.close)
    opened = float(candle.open)
    min_body = FAST_CONFIRM_MIN_BODY_RATIO if strict else MICRO_TRIGGER_MIN_BODY_RATIO
    min_rvol = FAST_CONFIRM_MIN_RVOL if strict else MICRO_TRIGGER_MIN_RVOL
    long_close = FAST_CONFIRM_CLOSE_LONG if strict else MICRO_TRIGGER_CLOSE_LONG
    short_close = FAST_CONFIRM_CLOSE_SHORT if strict else MICRO_TRIGGER_CLOSE_SHORT
    if setup.direction == Direction.LONG:
        close_in_trigger_band = close >= setup.trigger_price - close_buffer and close <= chase_limit
        ok = close > opened and close_in_trigger_band and body >= min_body and close_pos >= long_close and rvol >= min_rvol
    else:
        close_in_trigger_band = close <= setup.trigger_price + close_buffer and close >= chase_limit
        ok = close < opened and close_in_trigger_band and body >= min_body and close_pos <= short_close and rvol >= min_rvol
    return ok, {
        "1m_close": close,
        "1m_body_ratio": round(body, 4),
        "1m_close_pos": round(close_pos, 4),
        "1m_rvol": round(rvol, 4),
        "trigger_close_buffer": close_buffer,
        "chase_limit": chase_limit,
    }

def _micro_confirmation(
    setup: ArmedSetup,
    snapshot,
    *,
    chase_tolerance_atr: float,
    trigger_close_tolerance_atr: float,
    fast_confirm_enabled: bool,
    fast_confirm_max_age_seconds: float,
) -> tuple[bool, str, dict]:
    frames = getattr(snapshot, "timeframes", {}) or {}
    candles_1m = list(frames.get("1m", []) or [])
    executable = getattr(snapshot, "ask" if setup.direction == Direction.LONG else "bid", None)
    try:
        executable = float(executable)
    except (TypeError, ValueError):
        return False, "quote_unavailable", {}
    if not math.isfinite(executable) or executable <= 0:
        return False, "quote_unavailable", {}

    atr_value = float((setup.metadata or {}).get("atr_value") or 0.0)
    try:
        ask = float(getattr(snapshot, "ask", 0.0) or 0.0)
        bid = float(getattr(snapshot, "bid", 0.0) or 0.0)
        spread = max(ask - bid, 0.0) if ask > 0 and bid > 0 else 0.0
    except (TypeError, ValueError):
        spread = 0.0
    # A setup should not die because the executable quote moved a few ticks past
    # the original 5m zone. ATR is the primary tolerance. Spread can widen the
    # buffer slightly, but is capped so a bad/wide book never authorizes a late entry.
    atr_buffer = atr_value * max(chase_tolerance_atr, 0.0)
    if atr_value > 0:
        spread_buffer = min(spread * 1.5, atr_value * min(max(chase_tolerance_atr * 1.5, 0.0), 0.25))
    else:
        spread_buffer = spread * 1.5
    chase_buffer = max(atr_buffer, spread_buffer)
    trigger_close_buffer = atr_value * max(trigger_close_tolerance_atr, 0.0)

    # A one-tick / tiny-spread boundary miss must not turn a valid setup into
    # setup_chased. This tolerance is deliberately tiny and capped to 0.02 ATR.
    book_tick = _book_tick_size(snapshot)
    edge_raw = max(book_tick * 1.25, spread * 0.10, executable * 1e-9)
    chase_edge_tolerance = min(edge_raw, atr_value * CHASE_EDGE_MAX_ATR) if atr_value > 0 else edge_raw

    if setup.direction == Direction.LONG:
        chase_base_limit = setup.entry_zone_high + chase_buffer
        chase_limit = chase_base_limit + chase_edge_tolerance
        if executable <= setup.invalidation_price:
            return False, "setup_invalidated", {"price": executable}
        if executable > chase_limit:
            return False, "setup_chased", {
                "price": executable,
                "entry_zone_high": setup.entry_zone_high,
                "chase_limit": chase_limit,
                "chase_base_limit": chase_base_limit,
                "chase_buffer": chase_buffer,
                "chase_edge_tolerance": chase_edge_tolerance,
                "book_tick_size": book_tick,
            }
        crossed = executable >= setup.trigger_price
    else:
        chase_base_limit = setup.entry_zone_low - chase_buffer
        chase_limit = chase_base_limit - chase_edge_tolerance
        if executable >= setup.invalidation_price:
            return False, "setup_invalidated", {"price": executable}
        if executable < chase_limit:
            return False, "setup_chased", {
                "price": executable,
                "entry_zone_low": setup.entry_zone_low,
                "chase_limit": chase_limit,
                "chase_base_limit": chase_base_limit,
                "chase_buffer": chase_buffer,
                "chase_edge_tolerance": chase_edge_tolerance,
                "book_tick_size": book_tick,
            }
        crossed = executable <= setup.trigger_price
    if not crossed:
        return False, "waiting_trigger", {"price": executable, "trigger_price": setup.trigger_price}

    # A quote crossing alone is never enough. We always require CLOSED 1m evidence.
    if not candles_1m:
        return False, "waiting_1m_confirmation", {"price": executable}
    last = candles_1m[-1]
    micro_close_ms = int(getattr(last, "timestamp", 0) or 0) + 60_000
    armed_at_ms = int(setup.armed_at_ms)
    snapshot_ms = int(getattr(snapshot, "quote_received_ms", 0) or 0) or _now_ms()

    # Fast path: reuse only the immediately preceding CLOSED 1m candle.  It is
    # stricter than the normal path and additionally requires a healthy order book.
    # This closes the timing hole where a setup arms seconds after a strong 1m
    # confirmation and otherwise waits almost a full minute while price escapes.
    if micro_close_ms <= armed_at_ms:
        max_age_ms = int(max(0.0, fast_confirm_max_age_seconds) * 1000)
        prearm_age_ms = max(0, armed_at_ms - micro_close_ms)
        confirmation_age_ms = max(0, snapshot_ms - micro_close_ms)
        book_valid = bool(getattr(snapshot, "orderbook_valid", False)) and bool(
            getattr(snapshot, "bids", None)
        ) and bool(getattr(snapshot, "asks", None))
        strong_enough = float(setup.quality) >= FAST_CONFIRM_MIN_SETUP_QUALITY
        fast_ok, fast_diag = _closed_1m_quality(
            setup, last, candles_1m, close_buffer=trigger_close_buffer,
            chase_limit=chase_limit, strict=True,
        )
        eligible = (
            bool(fast_confirm_enabled)
            and max_age_ms > 0
            and prearm_age_ms <= max_age_ms
            and confirmation_age_ms <= max_age_ms
            and book_valid
            and strong_enough
            and fast_ok
        )
        if eligible:
            return True, "triggered", {
                "price": executable,
                **fast_diag,
                "confirmation_mode": "recent_prearm_closed_1m",
                "confirmation_age_ms": confirmation_age_ms,
                "prearm_age_ms": prearm_age_ms,
                "fast_confirm_max_age_ms": max_age_ms,
                "fast_confirm_setup_quality": float(setup.quality),
                "chase_base_limit": chase_base_limit,
                "chase_edge_tolerance": chase_edge_tolerance,
                "book_tick_size": book_tick,
            }
        return False, "waiting_fresh_1m_confirmation", {
            "price": executable,
            **fast_diag,
            "micro_close_ms": micro_close_ms,
            "armed_at_ms": armed_at_ms,
            "confirmation_age_ms": confirmation_age_ms,
            "prearm_age_ms": prearm_age_ms,
            "fast_confirm_max_age_ms": max_age_ms,
            "fast_confirm_book_valid": bool(book_valid),
            "fast_confirm_setup_quality": float(setup.quality),
            "fast_confirm_quality_ok": bool(fast_ok),
            "confirmation_mode": "waiting_new_closed_1m",
        }

    # Normal path: the 1m candle closed after arming.  Preserve the existing
    # invalidation and quality rules exactly.
    if setup.direction == Direction.LONG and float(last.low) <= setup.invalidation_price:
        return False, "setup_invalidated", {"price": executable, "1m_low": float(last.low)}
    if setup.direction == Direction.SHORT and float(last.high) >= setup.invalidation_price:
        return False, "setup_invalidated", {"price": executable, "1m_high": float(last.high)}
    ok, diag = _closed_1m_quality(
        setup, last, candles_1m, close_buffer=trigger_close_buffer,
        chase_limit=chase_limit, strict=False,
    )
    if not ok:
        return False, "waiting_1m_confirmation", {
            "price": executable, **diag,
            "confirmation_mode": "postarm_closed_1m",
            "chase_base_limit": chase_base_limit,
            "chase_edge_tolerance": chase_edge_tolerance,
            "book_tick_size": book_tick,
        }
    return True, "triggered", {
        "price": executable, **diag,
        "confirmation_mode": "postarm_closed_1m",
        "chase_base_limit": chase_base_limit,
        "chase_edge_tolerance": chase_edge_tolerance,
        "book_tick_size": book_tick,
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
    def __init__(
        self,
        ttl_seconds: float = 600.0,
        chase_tolerance_atr: float = 0.15,
        trigger_close_tolerance_atr: float = 0.08,
        consumed_ttl_seconds: float = 3600.0,
        fast_confirm_enabled: bool = True,
        fast_confirm_max_age_seconds: float = 30.0,
    ):
        self.ttl_seconds = max(60.0, float(ttl_seconds))
        self.chase_tolerance_atr = max(0.0, float(chase_tolerance_atr))
        self.trigger_close_tolerance_atr = max(0.0, float(trigger_close_tolerance_atr))
        self.consumed_ttl_seconds = max(self.ttl_seconds, float(consumed_ttl_seconds))
        self.fast_confirm_enabled = bool(fast_confirm_enabled)
        self.fast_confirm_max_age_seconds = max(0.0, float(fast_confirm_max_age_seconds))
        self._consumed_setups: dict[str, tuple[int, str]] = {}
        self.last_trace: dict[str, Any] = {}
        self.branch_trace: dict[str, dict[str, Any]] = {}

    def _prune_consumed(self, now_ms: int | None = None) -> None:
        now_ms = _now_ms() if now_ms is None else int(now_ms)
        expired = [setup_id for setup_id, (until_ms, _) in self._consumed_setups.items() if until_ms <= now_ms]
        for setup_id in expired:
            self._consumed_setups.pop(setup_id, None)

    def _is_consumed(self, setup_id: str, now_ms: int | None = None) -> tuple[bool, str | None]:
        self._prune_consumed(now_ms)
        item = self._consumed_setups.get(str(setup_id))
        return (item is not None, item[1] if item else None)

    def _consume(self, setup: ArmedSetup, reason: str, now_ms: int | None = None) -> None:
        now_ms = _now_ms() if now_ms is None else int(now_ms)
        until_ms = now_ms + int(self.consumed_ttl_seconds * 1000)
        self._consumed_setups[str(setup.setup_id)] = (until_ms, str(reason))
        # Defensive bound for long-running workers. Oldest entries are safe to drop:
        # the structural candle will also age out of discovery shortly afterwards.
        if len(self._consumed_setups) > 4096:
            oldest = sorted(self._consumed_setups.items(), key=lambda item: item[1][0])[:1024]
            for setup_id, _ in oldest:
                self._consumed_setups.pop(setup_id, None)

    def restore_consumed(self, setup_id: str, until_ms: int, reason: str = "restored") -> bool:
        """Restore a durable consumed-setup tombstone after a worker restart."""
        now_ms = _now_ms()
        until_ms = int(until_ms or 0)
        if not setup_id or until_ms <= now_ms:
            return False
        self._consumed_setups[str(setup_id)] = (until_ms, str(reason or "restored"))
        self._prune_consumed(now_ms)
        return True

    def consumed_record(self, setup_id: str) -> tuple[int, str] | None:
        """Return the current consumed TTL/reason for durable persistence."""
        self._prune_consumed()
        return self._consumed_setups.get(str(setup_id))

    def _expiry(self, snapshot) -> tuple[int, int]:
        armed_at = _now_ms()
        return armed_at, armed_at + int(self.ttl_seconds * 1000)

    @staticmethod
    def _active_regime(regime_metadata: dict | None) -> str:
        return str((regime_metadata or {}).get("active") or "UNKNOWN").upper()

    @staticmethod
    def _liquidity_trend_direction(regime, regime_metadata: dict | None) -> tuple[Direction | None, str]:
        """Resolve the only direction a liquidity sweep may trade.

        Liquidity Sweep is trend-following in KAELEON: bullish context permits
        LONG only, bearish context permits SHORT only. Neutral/unknown or
        conflicting regime-vs-feature direction blocks the strategy entirely.
        """
        raw_regime = getattr(getattr(regime, "direction", None), "value", getattr(regime, "direction", None))
        raw_regime = str(raw_regime or "").strip().upper()
        regime_side = (
            Direction.LONG if raw_regime in {"BULLISH", "LONG"}
            else Direction.SHORT if raw_regime in {"BEARISH", "SHORT"}
            else None
        )
        features = (regime_metadata or {}).get("features") or {}
        raw_bias = str(features.get("trend_bias") or "").strip().lower()
        bias_side = Direction.LONG if raw_bias == "long" else (Direction.SHORT if raw_bias == "short" else None)
        if regime_side is not None and bias_side is not None and regime_side != bias_side:
            return None, "liquidity_sweep_trend_conflict"
        side = regime_side or bias_side
        if side is None:
            return None, "liquidity_sweep_no_directional_trend"
        return side, "liquidity_sweep_trend_aligned"

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
        for retest_count in range(1, BREAKOUT_ARM_MAX_RETEST_BARS + 1):
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
            target_rr_cap = breakout_retest_target_rr()
            target, target_capped = cap_target_by_rr(trigger, stop, target, direction, target_rr_cap)
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
            fresh_q = clamp(1.0 - (retest_count - 1) / max(BREAKOUT_ARM_MAX_RETEST_BARS, 1), 0, 1)
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
                    "target_rr_cap": float(target_rr_cap),
                    "target_rr_capped": bool(target_capped),
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
        required_direction, trend_reason = self._liquidity_trend_direction(regime, regime_metadata)
        if required_direction is None:
            self.branch_trace["sweep"] = {"accepted": False, "reason": trend_reason}
            return None
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
            for direction_name in (("long",) if required_direction == Direction.LONG else ("short",)):
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
                target_rr_cap = liquidity_sweep_target_rr()
                target, target_capped = cap_target_by_rr(trigger, stop, target, direction, target_rr_cap)
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
                        "target_rr_cap": float(target_rr_cap),
                        "target_rr_capped": bool(target_capped),
                        "structural_rr_estimate": float(rr),
                        "stop_atr_5m": float(stop_atr),
                        "regime": self._active_regime(regime_metadata),
                        "trend_aligned": True,
                        "trend_direction_at_arm": required_direction.value,
                        "trend_alignment_reason": trend_reason,
                        "trend_alignment_guard_version": 1,
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
        # Strategy hierarchy: in TREND_CONTINUATION a valid BREAKOUT_RETEST has
        # first priority. LIQUIDITY_SWEEP is a secondary trend-following entry
        # only when no breakout/retest is armable. RANGE remains shadow-only.
        breakout_item = None
        if active == "TREND_CONTINUATION" or getattr(regime, "breakout_allowed", False):
            breakout_item = self._discover_breakout(regime, snapshot, symbol, timeframe, regime_metadata)
            if breakout_item:
                candidates.append(breakout_item)

        volatile_score = float(scores.get("VOLATILE_SWEEP") or 0.0)
        sweep_permitted = False
        if active == "RANGE":
            self.branch_trace["sweep"] = {"accepted": False, "reason": "regime_sweep_not_allowed"}
        elif active == "VOLATILE_SWEEP":
            sweep_permitted = bool(getattr(regime, "sweep_allowed", False))
            if not sweep_permitted:
                self.branch_trace["sweep"] = {"accepted": False, "reason": "regime_sweep_not_allowed"}
        elif active == "TREND_CONTINUATION":
            # Never let a sweep compete with a valid trend breakout. When breakout
            # is absent, volatility evidence may enable an aligned sweep fallback.
            sweep_permitted = breakout_item is None and volatile_score >= 2.0
            if breakout_item is not None:
                self.branch_trace["sweep"] = {"accepted": False, "reason": "primary_breakout_selected"}
            elif not sweep_permitted:
                self.branch_trace["sweep"] = {"accepted": False, "reason": "trend_liquidity_probe_not_met"}
        elif getattr(regime, "sweep_allowed", False):
            sweep_permitted = True

        if sweep_permitted:
            item = self._discover_sweep(regime, snapshot, symbol, timeframe, regime_metadata)
            if item:
                candidates.append(item)
        if candidates:
            available: list[ArmedSetup] = []
            consumed: list[tuple[ArmedSetup, str | None]] = []
            for candidate in candidates:
                was_consumed, consumed_reason = self._is_consumed(candidate.setup_id)
                if was_consumed:
                    consumed.append((candidate, consumed_reason))
                    branch = "breakout" if candidate.strategy == Strategy.BREAKOUT_RETEST else "sweep"
                    self.branch_trace[branch] = {
                        "accepted": False,
                        "reason": "setup_consumed_waiting_new_structure",
                        "setup_id": candidate.setup_id,
                        "consumed_reason": consumed_reason,
                    }
                else:
                    available.append(candidate)
            candidates = available
            if not candidates and consumed:
                self.last_trace = {
                    "accepted": False,
                    "reason": "setup_consumed_waiting_new_structure",
                    "consumed_setup_ids": [item.setup_id for item, _ in consumed],
                    "branches": dict(self.branch_trace),
                }
                return None
        if not candidates:
            primary = "no_armable_setup"
            sweep_reason = str((self.branch_trace.get("sweep") or {}).get("reason") or "")
            if active == "TREND_CONTINUATION" and sweep_reason in {
                "liquidity_sweep_trend_conflict", "liquidity_sweep_no_directional_trend"
            }:
                primary = sweep_reason
            elif active == "TREND_CONTINUATION" and self.branch_trace.get("breakout"):
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
        if setup.strategy == Strategy.LIQUIDITY_SWEEP:
            meta = dict(setup.metadata or {})
            aligned = meta.get("trend_aligned") is True and int(meta.get("trend_alignment_guard_version") or 0) >= 1
            armed_direction = str(meta.get("trend_direction_at_arm") or "").upper()
            if not aligned:
                self._consume(setup, "liquidity_sweep_legacy_unaligned_setup", now_ms)
                return "cancelled", None, {"reason": "liquidity_sweep_legacy_unaligned_setup"}
            if armed_direction != setup.direction.value:
                self._consume(setup, "liquidity_sweep_trend_mismatch", now_ms)
                return "cancelled", None, {
                    "reason": "liquidity_sweep_trend_mismatch",
                    "trend_direction_at_arm": armed_direction,
                    "setup_direction": setup.direction.value,
                }
        if now_ms >= int(setup.expires_at_ms):
            self._consume(setup, "setup_expired", now_ms)
            return "cancelled", None, {"reason": "setup_expired"}
        ok, reason, diag = _micro_confirmation(
            setup,
            snapshot,
            chase_tolerance_atr=self.chase_tolerance_atr,
            trigger_close_tolerance_atr=self.trigger_close_tolerance_atr,
            fast_confirm_enabled=self.fast_confirm_enabled,
            fast_confirm_max_age_seconds=self.fast_confirm_max_age_seconds,
        )
        if not ok:
            if reason in {"setup_invalidated", "setup_chased"}:
                self._consume(setup, reason, now_ms)
                return "cancelled", None, {"reason": reason, **diag}
            return "pending", None, {"reason": reason, **diag}
        executable = float(diag["price"])
        target = float(setup.target_price)
        stop = float(setup.stop_price)
        target_rr_cap = (
            liquidity_sweep_target_rr()
            if setup.strategy == Strategy.LIQUIDITY_SWEEP
            else breakout_retest_target_rr()
        )
        target, trigger_target_capped = cap_target_by_rr(
            executable, stop, target, setup.direction, target_rr_cap
        )
        geometry = (
            setup.direction == Direction.LONG and stop < executable < target
        ) or (
            setup.direction == Direction.SHORT and target < executable < stop
        )
        if not geometry:
            self._consume(setup, "trigger_invalid_geometry", now_ms)
            return "cancelled", None, {"reason": "trigger_invalid_geometry", **diag}
        rr = abs(target - executable) / max(abs(executable - stop), 1e-12)
        if rr < ARM_MIN_RR:
            self._consume(setup, "trigger_rr_too_low", now_ms)
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
            "target_rr_cap": target_rr_cap,
            "target_rr_capped_at_trigger": trigger_target_capped,
        }
        confirm_reason = (
            "micro_confirmation_fast_1m"
            if diag.get("confirmation_mode") == "recent_prearm_closed_1m"
            else "micro_confirmation_1m"
        )
        self._consume(setup, "setup_triggered", now_ms)
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
            reasons=tuple(setup.reasons) + (confirm_reason,),
            metadata=metadata,
        )
        return "triggered", intent, {"reason": confirm_reason, "execution_rr": rr, **diag}
