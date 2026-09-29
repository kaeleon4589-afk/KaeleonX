from __future__ import annotations

import math
import time
from dataclasses import replace
from typing import Any

from app.models.enums import Direction, Strategy
from app.models.trading import ArmedSetup, SetupWatch, TradeIntent
from app.regime.advanced import features as regime_features
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
BREAKOUT_ARM_MAX_RETEST_BARS = max(breakout.RETEST_MAX_BARS_AFTER_BREAKOUT, 5)
SWEEP_ENTRY_MAX_EXTENSION_ATR = 0.90
MICRO_TRIGGER_MIN_BODY_RATIO = 0.22
MICRO_TRIGGER_MIN_RVOL = 0.70
MICRO_TRIGGER_CLOSE_LONG = 0.60
MICRO_TRIGGER_CLOSE_SHORT = 0.40
# Evidence-driven post-arm fallback. The normal 1m rule remains unchanged. When a
# setup is already structurally strong, the live book is healthy and the closed 1m
# candle has a strong directional body, moderate volume may confirm instead of
# forcing the setup to wait until price has already escaped the valid RR window.
POSTARM_ADAPTIVE_MIN_SETUP_QUALITY = 72.0
POSTARM_ADAPTIVE_MIN_BODY_RATIO = 0.45
POSTARM_ADAPTIVE_MIN_RVOL = 0.50
POSTARM_ADAPTIVE_CLOSE_LONG = 0.72
POSTARM_ADAPTIVE_CLOSE_SHORT = 0.28
# Fast confirmation is intentionally stricter than the normal post-arm 1m path.
# It may reuse the most recent CLOSED 1m candle only when that candle closed
# immediately before arming and already showed strong directional acceptance.
FAST_CONFIRM_MIN_SETUP_QUALITY = 78.0
FAST_CONFIRM_MIN_BODY_RATIO = 0.45
FAST_CONFIRM_MIN_RVOL = 0.90
FAST_CONFIRM_CLOSE_LONG = 0.72
FAST_CONFIRM_CLOSE_SHORT = 0.28
CHASE_EDGE_MAX_ATR = 0.02
WATCH_BREAKOUT_MAX_AGE_BARS = 3
WATCH_SWEEP_PROXIMITY_ATR = 0.45

# v6.10 evidence-driven adaptive breakout-retest depth. Production stateful-watch
# telemetry clustered several otherwise trend-aligned retests around 0.089-0.101 ATR
# while the fixed 0.12 ATR floor kept them in WATCHING until they later invalidated.
# Keep 0.12 ATR as the default. Only a fresh (<=3 bars), clearly aligned 5m trend
# may use the narrower 0.085 ATR floor. All penetration, close invalidation, RR,
# stop, HTF exhaustion and post-arm confirmation guards remain unchanged.
RETEST_PULLBACK_BASE_ATR = 0.12
RETEST_PULLBACK_STRONG_TREND_ATR = 0.085
RETEST_PULLBACK_ADAPTIVE_MAX_BARS = 3
RETEST_PULLBACK_ADAPTIVE_MIN_ADX = 20.0
RETEST_PULLBACK_ADAPTIVE_MIN_EMA_ALIGNMENT = 0.75
RETEST_PULLBACK_ADAPTIVE_MIN_EMA_EDGE = 0.50


def _now_ms() -> int:
    return int(time.time() * 1000)


def _shape(candle) -> tuple[float, float]:
    span = max(float(candle.high) - float(candle.low), 1e-12)
    body = abs(float(candle.close) - float(candle.open)) / span
    pos = clamp((float(candle.close) - float(candle.low)) / span, 0.0, 1.0)
    return body, pos


def _snapshot_trend_direction(snapshot) -> tuple[Direction | None, dict]:
    """Resolve current 5m trend from fresh closed candles on the monitor snapshot.

    WATCHING and ARMED lifecycles must never keep trusting the direction captured
    when they were created.  This helper intentionally uses the same `trend_bias`
    feature that feeds RegimeEngine direction, but does not advance the regime
    state machine on every priority poll.
    """
    frames = getattr(snapshot, "timeframes", {}) or {}
    c5 = list(frames.get("5m", getattr(snapshot, "candles", [])) or [])
    if len(c5) < 205:
        return None, {
            "trend_context_status": "unavailable",
            "trend_context_bars": len(c5),
            "current_trend_bias": "unknown",
        }
    try:
        values = regime_features(c5, getattr(snapshot, "btc_candles", None))
    except Exception as exc:
        return None, {
            "trend_context_status": "error",
            "trend_context_bars": len(c5),
            "current_trend_bias": "unknown",
            "trend_context_error": f"{type(exc).__name__}: {exc}",
        }
    bias = str(values.get("trend_bias") or "neutral").strip().lower()
    direction = Direction.LONG if bias == "long" else (Direction.SHORT if bias == "short" else None)
    return direction, {
        "trend_context_status": "ok",
        "trend_context_bars": len(c5),
        "current_trend_bias": bias,
        "current_ema_alignment": float(values.get("ema_stack_alignment") or 0.0),
        "current_ema_alignment_edge": float(values.get("ema_alignment_edge") or 0.0),
        "current_adx": float(values.get("adx") or 0.0),
    }



def _adaptive_retest_pullback_requirement(
    direction: Direction, bars_since_breakout: int, trend_diag: dict
) -> tuple[float, dict]:
    """Return the minimum breakout pullback required for this live watch.

    The fixed 0.12 ATR rule remains the default. A shallower 0.085 ATR retest is
    allowed only when the *current* closed-5m context is still strongly aligned
    with the watch and the retest is fresh. This is intentionally narrow: it
    adapts one lower-bound gate and leaves every invalidation/quality/risk gate
    untouched.
    """
    status = str(trend_diag.get("trend_context_status") or "")
    adx_value = float(trend_diag.get("current_adx") or 0.0)
    ema_alignment = float(trend_diag.get("current_ema_alignment") or 0.0)
    ema_edge = float(trend_diag.get("current_ema_alignment_edge") or 0.0)
    directional_edge_ok = (
        ema_edge >= RETEST_PULLBACK_ADAPTIVE_MIN_EMA_EDGE
        if direction == Direction.LONG
        else ema_edge <= -RETEST_PULLBACK_ADAPTIVE_MIN_EMA_EDGE
    )
    eligible = (
        status == "ok"
        and 0 < int(bars_since_breakout) <= RETEST_PULLBACK_ADAPTIVE_MAX_BARS
        and adx_value >= RETEST_PULLBACK_ADAPTIVE_MIN_ADX
        and ema_alignment >= RETEST_PULLBACK_ADAPTIVE_MIN_EMA_ALIGNMENT
        and directional_edge_ok
    )
    required = RETEST_PULLBACK_STRONG_TREND_ATR if eligible else RETEST_PULLBACK_BASE_ATR
    return required, {
        "retest_pullback_mode": "strong_trend_adaptive" if eligible else "standard",
        "required_pullback_atr": required,
        "retest_pullback_base_atr": RETEST_PULLBACK_BASE_ATR,
        "retest_pullback_adaptive_floor_atr": RETEST_PULLBACK_STRONG_TREND_ATR,
        "retest_pullback_adaptive_eligible": eligible,
        "retest_pullback_adaptive_max_bars": RETEST_PULLBACK_ADAPTIVE_MAX_BARS,
        "retest_pullback_adaptive_min_adx": RETEST_PULLBACK_ADAPTIVE_MIN_ADX,
        "retest_pullback_adaptive_min_ema_alignment": RETEST_PULLBACK_ADAPTIVE_MIN_EMA_ALIGNMENT,
        "retest_pullback_adaptive_min_ema_edge": RETEST_PULLBACK_ADAPTIVE_MIN_EMA_EDGE,
    }


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
        direction_ok = close > opened
        standard_close_ok = close_pos >= long_close
        adaptive_close_ok = close_pos >= POSTARM_ADAPTIVE_CLOSE_LONG
    else:
        close_in_trigger_band = close <= setup.trigger_price + close_buffer and close >= chase_limit
        direction_ok = close < opened
        standard_close_ok = close_pos <= short_close
        adaptive_close_ok = close_pos <= POSTARM_ADAPTIVE_CLOSE_SHORT
    standard_shape_ok = direction_ok and body >= min_body and standard_close_ok
    ok = standard_shape_ok and close_in_trigger_band and rvol >= min_rvol
    adaptive_shape_ok = (
        direction_ok
        and close_in_trigger_band
        and body >= POSTARM_ADAPTIVE_MIN_BODY_RATIO
        and adaptive_close_ok
    )
    return ok, {
        "1m_close": close,
        "1m_body_ratio": round(body, 4),
        "1m_close_pos": round(close_pos, 4),
        "1m_rvol": round(rvol, 4),
        "1m_direction_ok": bool(direction_ok),
        "1m_close_in_trigger_band": bool(close_in_trigger_band),
        "1m_standard_shape_ok": bool(standard_shape_ok),
        "1m_adaptive_shape_ok": bool(adaptive_shape_ok),
        "1m_adaptive_volume_ok": bool(rvol >= POSTARM_ADAPTIVE_MIN_RVOL),
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
    book_valid = bool(getattr(snapshot, "orderbook_valid", False)) and bool(
        getattr(snapshot, "bids", None)
    ) and bool(getattr(snapshot, "asks", None))
    adaptive_ok = (
        not ok
        and float(setup.quality) >= POSTARM_ADAPTIVE_MIN_SETUP_QUALITY
        and bool(book_valid)
        and bool(diag.get("1m_adaptive_shape_ok"))
        and bool(diag.get("1m_adaptive_volume_ok"))
    )
    if not ok and not adaptive_ok:
        return False, "waiting_1m_confirmation", {
            "price": executable, **diag,
            "confirmation_mode": "postarm_closed_1m",
            "postarm_adaptive_candidate": bool(
                diag.get("1m_adaptive_shape_ok") and diag.get("1m_adaptive_volume_ok")
            ),
            "postarm_adaptive_book_valid": bool(book_valid),
            "postarm_adaptive_setup_quality_ok": bool(
                float(setup.quality) >= POSTARM_ADAPTIVE_MIN_SETUP_QUALITY
            ),
            "postarm_adaptive_min_setup_quality": POSTARM_ADAPTIVE_MIN_SETUP_QUALITY,
            "postarm_adaptive_min_rvol": POSTARM_ADAPTIVE_MIN_RVOL,
            "chase_base_limit": chase_base_limit,
            "chase_edge_tolerance": chase_edge_tolerance,
            "book_tick_size": book_tick,
        }
    confirmation_mode = (
        "postarm_closed_1m_strong_shape" if adaptive_ok else "postarm_closed_1m"
    )
    return True, "triggered", {
        "price": executable, **diag,
        "confirmation_mode": confirmation_mode,
        "postarm_adaptive_used": bool(adaptive_ok),
        "postarm_adaptive_book_valid": bool(book_valid),
        "postarm_adaptive_setup_quality": float(setup.quality),
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
        watch_ttl_seconds: float = 1800.0,
    ):
        self.ttl_seconds = max(60.0, float(ttl_seconds))
        self.chase_tolerance_atr = max(0.0, float(chase_tolerance_atr))
        self.trigger_close_tolerance_atr = max(0.0, float(trigger_close_tolerance_atr))
        self.consumed_ttl_seconds = max(self.ttl_seconds, float(consumed_ttl_seconds))
        self.fast_confirm_enabled = bool(fast_confirm_enabled)
        self.fast_confirm_max_age_seconds = max(0.0, float(fast_confirm_max_age_seconds))
        self.watch_ttl_seconds = max(300.0, float(watch_ttl_seconds))
        self._consumed_setups: dict[str, tuple[int, str]] = {}
        self.watch_trace: dict[str, Any] = {}
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
        # One directional authority for the whole engine: regime direction +
        # trend_bias select LONG/SHORT. 1H/15M remain quality confirmations.
        required_direction, trend_reason = self._liquidity_trend_direction(regime, regime_metadata)
        if required_direction is None:
            self.branch_trace["breakout"] = {
                "accepted": False, "reason": "breakout_no_directional_trend",
                "trend_reason": trend_reason, "bias_1h": bias1h, "bias_15m": bias15,
            }
            return None
        direction_name = "long" if required_direction == Direction.LONG else "short"
        explicit_biases = [x for x in (bias1h, bias15) if x != "none"]
        if any(x != direction_name for x in explicit_biases):
            self.branch_trace["breakout"] = {
                "accepted": False, "reason": "mtf_bias_conflict_with_trend",
                "trend_direction": required_direction.value,
                "bias_1h": bias1h, "bias_15m": bias15,
            }
            return None
        exhausted, exhaustion_diag = breakout._htf_exhaustion(direction_name, tf1h, tf15)
        if exhausted:
            self.branch_trace["breakout"] = {"accepted": False, "reason": "higher_timeframe_move_exhausted", **exhaustion_diag}
            return None

        o, h, l, c, v = tf5["o"], tf5["h"], tf5["l"], tf5["c"], tf5["v"]
        i = len(c) - 1
        # Compute the live 5m trend diagnostics once per discovery pass. The
        # adaptive retest rule consumes these diagnostics for every candidate
        # without recalculating regime features inside the retest loop.
        _, direct_trend_diag = _snapshot_trend_direction(snapshot)
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
            # Keep direct discovery consistent with the stateful WATCHING path.
            # v6.10 originally applied the 0.085 ATR strong-trend allowance only
            # after a SetupWatch existed, which created a lifecycle gap: a setup
            # first seen after the watch-discovery window could still be rejected
            # here by the old hard-coded 0.12 ATR floor.
            required_pullback, pullback_rule_diag = _adaptive_retest_pullback_requirement(
                direction, retest_count, direct_trend_diag
            )
            if not (touch and no_deep and closes_valid and last_near) or pullback < required_pullback:
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
                    "retest_pullback_atr": float(pullback),
                    **pullback_rule_diag,
                    "breakout_rvol": float(rvol),
                    "breakout_age_bars": int(retest_count),
                    "structural_target_price": float(structural_target),
                    "target_front_run_ratio": float(target_ratio),
                    "target_rr_cap": float(target_rr_cap),
                    "target_rr_capped": bool(target_capped),
                    "structural_rr_estimate": float(rr),
                    "stop_atr_5m": float(stop_atr),
                    "regime": self._active_regime(regime_metadata),
                    "trend_revalidation_required": True,
                    "trend_direction_at_arm": direction.value,
                    "trend_revalidation_guard_version": 2,
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
                        "trend_revalidation_required": True,
                        "trend_revalidation_guard_version": 2,
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


    def _watch_expiry(self) -> tuple[int, int]:
        created = _now_ms()
        return created, created + int(self.watch_ttl_seconds * 1000)

    def _discover_breakout_watch(self, regime, snapshot, symbol: str, timeframe: str,
                                 regime_metadata: dict | None) -> SetupWatch | None:
        frames = getattr(snapshot, "timeframes", {}) or {}
        c5 = list(frames.get("5m", getattr(snapshot, "candles", [])) or [])
        c15 = list(frames.get("15m", []) or [])
        c1h = list(frames.get("1h", []) or [])
        ok, _ = candle_quality(c5, breakout.MIN_CANDLES_REQUIRED, breakout.MIN_NONZERO_VOLUME_RATIO)
        if not ok or len(c15) < 200 or len(c1h) < 200:
            self.watch_trace["breakout"] = {"accepted": False, "reason": "insufficient_or_bad_mtf_data"}
            return None
        tf5, tf15, tf1h = breakout._tf_values(c5), breakout._tf_values(c15), breakout._tf_values(c1h)
        atr5 = float(tf5["atr"])
        close5 = float(tf5["c"][-1])
        if atr5 <= 0:
            self.watch_trace["breakout"] = {"accepted": False, "reason": "invalid_atr"}
            return None
        atr_pct = atr5 / max(close5, 1e-12)
        if not breakout.ATR_PCT_MIN <= atr_pct <= breakout.ATR_PCT_MAX:
            self.watch_trace["breakout"] = {"accepted": False, "reason": "atr_out_of_range", "atr_pct": atr_pct}
            return None

        required_direction, trend_reason = self._liquidity_trend_direction(regime, regime_metadata)
        if required_direction is None:
            self.watch_trace["breakout"] = {"accepted": False, "reason": "breakout_no_directional_trend", "trend_reason": trend_reason}
            return None
        direction_name = "long" if required_direction == Direction.LONG else "short"
        bias1h, diag1h = breakout._bias(tf1h, adx_min=breakout.H1_ADX_MIN)
        bias15, diag15 = breakout._bias(tf15, adx_min=breakout.M15_ADX_MIN)
        explicit = [x for x in (bias1h, bias15) if x != "none"]
        if any(x != direction_name for x in explicit):
            self.watch_trace["breakout"] = {
                "accepted": False, "reason": "mtf_bias_conflict_with_trend",
                "trend_direction": required_direction.value, "bias_1h": bias1h, "bias_15m": bias15,
            }
            return None
        exhausted, exhaustion_diag = breakout._htf_exhaustion(direction_name, tf1h, tf15)
        if exhausted:
            self.watch_trace["breakout"] = {"accepted": False, "reason": "higher_timeframe_move_exhausted", **exhaustion_diag}
            return None

        o, h, l, c, v = tf5["o"], tf5["h"], tf5["l"], tf5["c"], tf5["v"]
        i = len(c) - 1
        for age in range(0, WATCH_BREAKOUT_MAX_AGE_BARS + 1):
            idx = i - age
            if idx <= breakout.STRUCTURE_LOOKBACK_BARS:
                continue
            start = idx - breakout.STRUCTURE_LOOKBACK_BARS
            structural_level = max(h[start:idx]) if direction_name == "long" else min(l[start:idx])
            body, close_pos = breakout._candle_shape(o, h, l, c, idx)
            rvol = relative_volume(v, idx)
            extension = abs(float(c[idx]) - float(structural_level)) / atr5
            if direction_name == "long":
                breakout_ok = (
                    float(c[idx]) > float(o[idx])
                    and float(c[idx]) >= float(structural_level) + atr5 * breakout.BREAKOUT_CLOSE_BUFFER_ATR
                    and body >= breakout.BREAKOUT_MIN_BODY_RATIO
                    and close_pos >= breakout.BREAKOUT_CLOSE_POS_LONG_MIN
                    and rvol >= breakout.BREAKOUT_MIN_RVOL
                    and extension <= breakout.BREAKOUT_MAX_EXTENSION_ATR
                )
            else:
                breakout_ok = (
                    float(c[idx]) < float(o[idx])
                    and float(c[idx]) <= float(structural_level) - atr5 * breakout.BREAKOUT_CLOSE_BUFFER_ATR
                    and body >= breakout.BREAKOUT_MIN_BODY_RATIO
                    and close_pos <= breakout.BREAKOUT_CLOSE_POS_SHORT_MAX
                    and rvol >= breakout.BREAKOUT_MIN_RVOL
                    and extension <= breakout.BREAKOUT_MAX_EXTENSION_ATR
                )
            if not breakout_ok:
                continue
            created, expires = self._watch_expiry()
            candle_ts = int(getattr(c5[idx], "timestamp", created))
            watch_id = f"{symbol}:BRW:{candle_ts}:{required_direction.value}"
            watch = SetupWatch(
                watch_id=watch_id,
                symbol=symbol, strategy=Strategy.BREAKOUT_RETEST, direction=required_direction,
                created_at_ms=created, expires_at_ms=expires, timeframe=timeframe,
                reasons=("breakout_detected", "waiting_retest"),
                metadata={
                    "lifecycle_id": watch_id,
                    "lifecycle_origin": "breakout_precursor",
                    "watch_model": "breakout_retest_watch_v2_stateful",
                    "breakout_candle_ts": candle_ts,
                    "structural_level": float(structural_level),
                    "atr_value_at_watch": atr5,
                    "breakout_rvol": float(rvol),
                    "breakout_body_ratio": float(body),
                    "breakout_close_pos": float(close_pos),
                    "trend_direction": required_direction.value,
                    "trend_reason": trend_reason,
                    "bias_1h": bias1h, "bias_15m": bias15,
                    "risk_multiplier": max(float(getattr(regime, "risk_multiplier", 1.0) or 1.0), 0.65),
                    "active_regime": self._active_regime(regime_metadata),
                    **exhaustion_diag,
                },
            )
            self.watch_trace["breakout"] = {
                "accepted": True, "reason": "breakout_detected_waiting_retest",
                "watch_id": watch.watch_id, "direction": required_direction.value,
                "structural_level": float(structural_level), "breakout_age_bars": age,
            }
            return watch
        self.watch_trace["breakout"] = {"accepted": False, "reason": "breakout_not_detected"}
        return None

    def _discover_sweep_watch(self, regime, snapshot, symbol: str, timeframe: str,
                              regime_metadata: dict | None) -> SetupWatch | None:
        required_direction, trend_reason = self._liquidity_trend_direction(regime, regime_metadata)
        if required_direction is None:
            self.watch_trace["sweep"] = {"accepted": False, "reason": trend_reason}
            return None
        frames = getattr(snapshot, "timeframes", {}) or {}
        c5 = list(frames.get("5m", getattr(snapshot, "candles", [])) or [])
        ok, _ = candle_quality(c5, sweep.MIN_CANDLES_REQUIRED, sweep.MIN_NONZERO_VOLUME_RATIO)
        if not ok:
            self.watch_trace["sweep"] = {"accepted": False, "reason": "bad_5m_candle_quality"}
            return None
        o, h, l, c, v = extract(c5)
        i = len(c) - 1
        atr5 = atr(h, l, c, 14)
        if atr5 <= 0:
            self.watch_trace["sweep"] = {"accepted": False, "reason": "invalid_atr"}
            return None
        atr_pct = float(atr5) / max(float(c[-1]), 1e-12)
        if not sweep.ATR_PCT_MIN <= atr_pct <= sweep.ATR_PCT_MAX:
            self.watch_trace["sweep"] = {"accepted": False, "reason": "atr_out_of_range", "atr_pct": atr_pct}
            return None
        left = max(0, i - sweep.SWEEP_LOOKBACK)
        if i - left < 12:
            self.watch_trace["sweep"] = {"accepted": False, "reason": "insufficient_liquidity_history"}
            return None
        if required_direction == Direction.LONG:
            level = min(l[left:i])
            proximity = (float(l[i]) - float(level)) / atr5
            directional_close_distance = (float(c[i]) - float(level)) / atr5
        else:
            level = max(h[left:i])
            proximity = (float(level) - float(h[i])) / atr5
            directional_close_distance = (float(level) - float(c[i])) / atr5
        # Start following before the sweep completes. A low/high within 0.45 ATR
        # of the liquidity pool is close enough to justify priority monitoring,
        # but prices already far through the level are left to normal discovery.
        if proximity > WATCH_SWEEP_PROXIMITY_ATR or directional_close_distance < -0.70:
            self.watch_trace["sweep"] = {
                "accepted": False, "reason": "sweep_level_not_reached",
                "proximity_atr": float(proximity), "liquidity_level": float(level),
            }
            return None
        created, expires = self._watch_expiry()
        watch_id = f"{symbol}:LSW:{int(getattr(c5[i], 'timestamp', created))}:{required_direction.value}"
        watch = SetupWatch(
            watch_id=watch_id,
            symbol=symbol, strategy=Strategy.LIQUIDITY_SWEEP, direction=required_direction,
            created_at_ms=created, expires_at_ms=expires, timeframe=timeframe,
            reasons=("trend_aligned_liquidity_pool_near", "waiting_sweep_reclaim"),
            metadata={
                "lifecycle_id": watch_id,
                "lifecycle_origin": "liquidity_precursor",
                "watch_model": "liquidity_sweep_watch_v2_stateful",
                "watch_candle_ts": int(getattr(c5[i], "timestamp", created)),
                "liquidity_level": float(level), "atr_value_at_watch": float(atr5),
                "proximity_atr": float(proximity), "trend_direction": required_direction.value,
                "trend_reason": trend_reason,
                "risk_multiplier": max(float(getattr(regime, "risk_multiplier", 1.0) or 1.0), 0.65),
                "active_regime": self._active_regime(regime_metadata),
            },
        )
        self.watch_trace["sweep"] = {
            "accepted": True, "reason": "liquidity_level_near_waiting_sweep",
            "watch_id": watch.watch_id, "direction": required_direction.value,
            "liquidity_level": float(level), "proximity_atr": float(proximity),
        }
        return watch

    def discover_watch(self, regime, snapshot, symbol: str, timeframe: str,
                       regime_metadata: dict | None = None) -> SetupWatch | None:
        self.watch_trace = {}
        active = self._active_regime(regime_metadata)
        if getattr(regime, "hard_block", False) and active == "UNKNOWN":
            self.watch_trace = {"accepted": False, "reason": "regime_unknown_hard_block"}
            return None
        # BREAKOUT_RETEST gets first-class priority in trend continuation: follow
        # the breakout immediately, rather than hoping the rotating scanner lands
        # on the symbol again after the retest has already completed.
        if active == "TREND_CONTINUATION" or bool(getattr(regime, "breakout_allowed", False)):
            item = self._discover_breakout_watch(regime, snapshot, symbol, timeframe, regime_metadata)
            if item is not None:
                return item
        if active == "RANGE":
            self.watch_trace["sweep"] = {"accepted": False, "reason": "regime_sweep_not_allowed"}
            return None
        if active == "VOLATILE_SWEEP" and not bool(getattr(regime, "sweep_allowed", False)):
            self.watch_trace["sweep"] = {"accepted": False, "reason": "regime_sweep_not_allowed"}
            return None
        if active in {"TREND_CONTINUATION", "VOLATILE_SWEEP"} or bool(getattr(regime, "sweep_allowed", False)):
            return self._discover_sweep_watch(regime, snapshot, symbol, timeframe, regime_metadata)
        return None

    @staticmethod
    def _watch_origin_index(watch: SetupWatch, candles, metadata_key: str) -> int | None:
        raw = int((watch.metadata or {}).get(metadata_key) or 0)
        if raw <= 0:
            # v6.6/v6.8 persisted watches did not store watch_candle_ts for
            # Liquidity Sweep. The timestamp is still encoded in the watch id.
            try:
                raw = int(str(watch.watch_id).split(":")[2])
            except (ValueError, IndexError):
                raw = 0
        if raw <= 0:
            return None
        return next(
            (idx for idx, candle in enumerate(candles)
             if int(getattr(candle, "timestamp", 0) or 0) == raw),
            None,
        )

    @staticmethod
    def _watch_trend_guard(watch: SetupWatch, snapshot) -> tuple[str, dict]:
        current_direction, trend_diag = _snapshot_trend_direction(snapshot)
        if trend_diag.get("trend_context_status") != "ok":
            return "pending", {"reason": "watch_trend_context_unavailable", **trend_diag}
        if current_direction is None:
            return "cancelled", {"reason": "watch_direction_lost", **trend_diag}
        if current_direction != watch.direction:
            return "cancelled", {
                "reason": "watch_direction_flipped",
                "watch_direction": watch.direction.value,
                "current_trend_direction": current_direction.value,
                **trend_diag,
            }
        return "ok", {
            "watch_direction": watch.direction.value,
            "current_trend_direction": current_direction.value,
            **trend_diag,
        }

    def _advance_breakout_watch(self, watch: SetupWatch, snapshot) -> tuple[str, ArmedSetup | None, dict]:
        frames = getattr(snapshot, "timeframes", {}) or {}
        c5 = list(frames.get("5m", getattr(snapshot, "candles", [])) or [])
        c15 = list(frames.get("15m", []) or [])
        c1h = list(frames.get("1h", []) or [])
        if len(c5) < breakout.MIN_CANDLES_REQUIRED or len(c15) < 200 or len(c1h) < 200:
            return "pending", None, {
                "reason": "watch_waiting_mtf_data",
                "bars_5m": len(c5), "bars_15m": len(c15), "bars_1h": len(c1h),
            }
        ok, _ = candle_quality(c5, breakout.MIN_CANDLES_REQUIRED, breakout.MIN_NONZERO_VOLUME_RATIO)
        if not ok:
            return "pending", None, {"reason": "watch_bad_5m_candle_quality"}

        guard_status, guard_diag = self._watch_trend_guard(watch, snapshot)
        if guard_status != "ok":
            return guard_status, None, {"watch_id": watch.watch_id, **guard_diag}

        tf5, tf15, tf1h = breakout._tf_values(c5), breakout._tf_values(c15), breakout._tf_values(c1h)
        o, h, l, c, v = tf5["o"], tf5["h"], tf5["l"], tf5["c"], tf5["v"]
        atr5 = float(tf5["atr"])
        if atr5 <= 0:
            return "pending", None, {"reason": "invalid_atr", "watch_id": watch.watch_id, **guard_diag}
        atr_pct = atr5 / max(float(c[-1]), 1e-12)
        if not breakout.ATR_PCT_MIN <= atr_pct <= breakout.ATR_PCT_MAX:
            return "cancelled", None, {
                "reason": "atr_out_of_range", "watch_id": watch.watch_id,
                "atr_pct": atr_pct, **guard_diag,
            }

        direction_name = "long" if watch.direction == Direction.LONG else "short"
        bias1h, diag1h = breakout._bias(tf1h, adx_min=breakout.H1_ADX_MIN)
        bias15, diag15 = breakout._bias(tf15, adx_min=breakout.M15_ADX_MIN)
        explicit = [x for x in (bias1h, bias15) if x != "none"]
        if any(x != direction_name for x in explicit):
            return "cancelled", None, {
                "reason": "mtf_bias_conflict_with_watch", "watch_id": watch.watch_id,
                "watch_direction": watch.direction.value, "bias_1h": bias1h, "bias_15m": bias15,
                **guard_diag,
            }
        exhausted, exhaustion_diag = breakout._htf_exhaustion(direction_name, tf1h, tf15)
        if exhausted:
            return "cancelled", None, {
                "reason": "higher_timeframe_move_exhausted", "watch_id": watch.watch_id,
                **guard_diag, **exhaustion_diag,
            }

        origin_idx = self._watch_origin_index(watch, c5, "breakout_candle_ts")
        if origin_idx is None:
            return "cancelled", None, {
                "reason": "breakout_origin_missing", "watch_id": watch.watch_id, **guard_diag,
            }
        i = len(c5) - 1
        bars_since = i - origin_idx
        if bars_since <= 0:
            return "pending", None, {
                "reason": "waiting_retest_touch", "watch_id": watch.watch_id,
                "bars_since_breakout": bars_since, **guard_diag,
            }
        if bars_since > BREAKOUT_ARM_MAX_RETEST_BARS:
            return "cancelled", None, {
                "reason": "breakout_retest_window_expired", "watch_id": watch.watch_id,
                "bars_since_breakout": bars_since, "max_retest_bars": BREAKOUT_ARM_MAX_RETEST_BARS,
                **guard_diag,
            }

        structural_level = float((watch.metadata or {}).get("structural_level") or 0.0)
        if structural_level <= 0:
            return "cancelled", None, {"reason": "breakout_structural_level_missing", "watch_id": watch.watch_id}
        retest_indices = list(range(origin_idx + 1, i + 1))
        breakout_close = float(c[origin_idx])
        breakout_rvol = float((watch.metadata or {}).get("breakout_rvol") or relative_volume(v, origin_idx))
        max_close_distance = max(breakout.RETEST_MAX_CLOSE_DISTANCE_ATR, 0.34)
        required_pullback, pullback_rule_diag = _adaptive_retest_pullback_requirement(
            watch.direction, bars_since, guard_diag
        )

        if watch.direction == Direction.LONG:
            extreme = min(float(l[j]) for j in retest_indices)
            touch_boundary = structural_level + atr5 * breakout.RETEST_TOUCH_TOL_ATR
            touch = extreme <= touch_boundary
            touch_distance_atr = max(0.0, extreme - touch_boundary) / atr5
            if not touch:
                return "pending", None, {
                    "reason": "waiting_retest_touch", "watch_id": watch.watch_id,
                    "structural_level": structural_level, "nearest_retest_low": extreme,
                    "touch_distance_atr": touch_distance_atr, "bars_since_breakout": bars_since,
                    **guard_diag,
                }
            if extreme < structural_level - atr5 * breakout.RETEST_MAX_PENETRATION_ATR:
                return "cancelled", None, {
                    "reason": "retest_too_deep", "watch_id": watch.watch_id,
                    "structural_level": structural_level, "retest_extreme": extreme,
                    "penetration_atr": (structural_level - extreme) / atr5, **guard_diag,
                }
            invalid_closes = [j for j in retest_indices if float(c[j]) < structural_level - atr5 * breakout.RETEST_CLOSE_INVALIDATION_ATR]
            if invalid_closes:
                return "cancelled", None, {
                    "reason": "retest_close_invalidated", "watch_id": watch.watch_id,
                    "invalid_close": float(c[invalid_closes[-1]]), "structural_level": structural_level,
                    **guard_diag,
                }
            pullback = max(0.0, breakout_close - extreme) / atr5
            if pullback < required_pullback:
                return "pending", None, {
                    "reason": "retest_pullback_insufficient", "watch_id": watch.watch_id,
                    "pullback_atr": pullback, "bars_since_breakout": bars_since,
                    **pullback_rule_diag, **guard_diag,
                }
            close_distance_atr = abs(float(c[i]) - structural_level) / atr5
            if close_distance_atr > max_close_distance:
                return "cancelled", None, {
                    "reason": "retest_too_extended", "watch_id": watch.watch_id,
                    "close_distance_atr": close_distance_atr,
                    "max_close_distance_atr": max_close_distance, **guard_diag,
                }
            trigger = max(structural_level + atr5 * 0.04, float(c[i]) + atr5 * 0.02)
            stop = extreme - atr5 * breakout.MTF_SL_BUFFER_ATR
            entry_zone_low = trigger
            entry_zone_high = structural_level + atr5 * BREAKOUT_ENTRY_MAX_EXTENSION_ATR
        else:
            extreme = max(float(h[j]) for j in retest_indices)
            touch_boundary = structural_level - atr5 * breakout.RETEST_TOUCH_TOL_ATR
            touch = extreme >= touch_boundary
            touch_distance_atr = max(0.0, touch_boundary - extreme) / atr5
            if not touch:
                return "pending", None, {
                    "reason": "waiting_retest_touch", "watch_id": watch.watch_id,
                    "structural_level": structural_level, "nearest_retest_high": extreme,
                    "touch_distance_atr": touch_distance_atr, "bars_since_breakout": bars_since,
                    **guard_diag,
                }
            if extreme > structural_level + atr5 * breakout.RETEST_MAX_PENETRATION_ATR:
                return "cancelled", None, {
                    "reason": "retest_too_deep", "watch_id": watch.watch_id,
                    "structural_level": structural_level, "retest_extreme": extreme,
                    "penetration_atr": (extreme - structural_level) / atr5, **guard_diag,
                }
            invalid_closes = [j for j in retest_indices if float(c[j]) > structural_level + atr5 * breakout.RETEST_CLOSE_INVALIDATION_ATR]
            if invalid_closes:
                return "cancelled", None, {
                    "reason": "retest_close_invalidated", "watch_id": watch.watch_id,
                    "invalid_close": float(c[invalid_closes[-1]]), "structural_level": structural_level,
                    **guard_diag,
                }
            pullback = max(0.0, extreme - breakout_close) / atr5
            if pullback < required_pullback:
                return "pending", None, {
                    "reason": "retest_pullback_insufficient", "watch_id": watch.watch_id,
                    "pullback_atr": pullback, "bars_since_breakout": bars_since,
                    **pullback_rule_diag, **guard_diag,
                }
            close_distance_atr = abs(float(c[i]) - structural_level) / atr5
            if close_distance_atr > max_close_distance:
                return "cancelled", None, {
                    "reason": "retest_too_extended", "watch_id": watch.watch_id,
                    "close_distance_atr": close_distance_atr,
                    "max_close_distance_atr": max_close_distance, **guard_diag,
                }
            trigger = min(structural_level - atr5 * 0.04, float(c[i]) - atr5 * 0.02)
            stop = extreme + atr5 * breakout.MTF_SL_BUFFER_ATR
            entry_zone_low = structural_level - atr5 * BREAKOUT_ENTRY_MAX_EXTENSION_ATR
            entry_zone_high = trigger

        stop_atr = abs(trigger - stop) / atr5
        if stop_atr < ARM_MIN_STOP_ATR:
            return "pending", None, {
                "reason": "retest_stop_too_tight", "watch_id": watch.watch_id,
                "stop_atr": stop_atr, "min_stop_atr": ARM_MIN_STOP_ATR, **guard_diag,
            }
        if stop_atr > ARM_MAX_STOP_ATR:
            return "cancelled", None, {
                "reason": "retest_stop_too_wide", "watch_id": watch.watch_id,
                "stop_atr": stop_atr, "max_stop_atr": ARM_MAX_STOP_ATR, **guard_diag,
            }

        structural_target = breakout._structure_target(watch.direction, trigger, h, l, tf15["h"], tf15["l"])
        target, target_ratio = front_run_target(trigger, structural_target, watch.direction)
        target_rr_cap = breakout_retest_target_rr()
        target, target_capped = cap_target_by_rr(trigger, stop, target, watch.direction, target_rr_cap)
        rr = abs(target - trigger) / max(abs(trigger - stop), 1e-12)
        if target <= 0 or rr < ARM_MIN_RR:
            return "pending", None, {
                "reason": "retest_rr_too_low", "watch_id": watch.watch_id,
                "structural_rr_estimate": rr, "min_rr": ARM_MIN_RR, **guard_diag,
            }
        path = abs(structural_target - structural_level)
        consumed = abs(trigger - structural_level) / max(path, 1e-12)
        if path >= atr5 * 0.45 and consumed > 0.58:
            return "cancelled", None, {
                "reason": "retest_too_extended", "watch_id": watch.watch_id,
                "impulse_consumed_ratio": consumed, **guard_diag,
            }

        htf_q = clamp(((float(diag1h.get("adx", 0)) - 12) + (float(diag15.get("adx", 0)) - 10)) / 30, 0, 1)
        breakout_q = clamp((breakout_rvol - 0.8) / 1.0, 0, 1)
        rr_q = clamp((rr - ARM_MIN_RR) / 1.6, 0, 1)
        fresh_q = clamp(1.0 - (bars_since - 1) / max(BREAKOUT_ARM_MAX_RETEST_BARS, 1), 0, 1)
        score = round(58 + 42 * clamp(0.34 * htf_q + 0.22 * breakout_q + 0.26 * rr_q + 0.18 * fresh_q, 0, 1), 2)
        if score < BREAKOUT_ARM_MIN_SCORE:
            return "pending", None, {
                "reason": "retest_quality_below_arm_threshold", "watch_id": watch.watch_id,
                "quality": score, "min_quality": BREAKOUT_ARM_MIN_SCORE, **guard_diag,
            }

        armed_at, expires_at = self._expiry(snapshot)
        lifecycle_id = str((watch.metadata or {}).get("lifecycle_id") or watch.watch_id)
        setup = ArmedSetup(
            setup_id=f"{watch.symbol}:BR:{int(getattr(c5[origin_idx], 'timestamp', armed_at))}:{watch.direction.value}",
            symbol=watch.symbol, strategy=Strategy.BREAKOUT_RETEST, direction=watch.direction,
            armed_at_ms=armed_at, expires_at_ms=expires_at,
            trigger_price=float(trigger), invalidation_price=float(stop), stop_price=float(stop),
            target_price=float(target), entry_zone_low=float(min(entry_zone_low, entry_zone_high)),
            entry_zone_high=float(max(entry_zone_low, entry_zone_high)), quality=score,
            risk_multiplier=max(float((watch.metadata or {}).get("risk_multiplier") or 1.0), 0.65),
            timeframe=watch.timeframe,
            reasons=("breakout_confirmed_5m", "retest_complete", "awaiting_micro_confirmation"),
            metadata={
                "strategy_model": "stateful_breakout_retest_watch_v2",
                "atr_value": atr5, "atr_pct": atr_pct,
                "structural_level": structural_level, "retest_extreme": float(extreme),
                "retest_pullback_atr": float(pullback), **pullback_rule_diag,
                "breakout_rvol": breakout_rvol, "breakout_age_bars": int(bars_since),
                "structural_target_price": float(structural_target),
                "target_front_run_ratio": float(target_ratio), "target_rr_cap": float(target_rr_cap),
                "target_rr_capped": bool(target_capped), "structural_rr_estimate": float(rr),
                "stop_atr_5m": float(stop_atr), "regime": str((watch.metadata or {}).get("active_regime") or "TREND_CONTINUATION"),
                "trend_revalidation_required": True, "trend_direction_at_arm": watch.direction.value,
                "trend_revalidation_guard_version": 2,
                "lifecycle_id": lifecycle_id,
                "lifecycle_origin": str((watch.metadata or {}).get("lifecycle_origin") or "breakout_precursor"),
                "origin_watch_id": watch.watch_id, "watch_created_at_ms": int(watch.created_at_ms),
                "watch_promoted_at_ms": _now_ms(), "breakout_candle_ts": int(getattr(c5[origin_idx], "timestamp", 0) or 0),
                **guard_diag, **exhaustion_diag,
            },
        )
        return "armed", setup, {
            "reason": "retest_complete", "watch_id": watch.watch_id, "lifecycle_id": lifecycle_id,
            "quality": score, "bars_since_breakout": bars_since, "structural_rr_estimate": rr,
            "retest_pullback_atr": float(pullback), **pullback_rule_diag, **guard_diag,
        }

    def _advance_sweep_watch(self, watch: SetupWatch, snapshot) -> tuple[str, ArmedSetup | None, dict]:
        frames = getattr(snapshot, "timeframes", {}) or {}
        c5 = list(frames.get("5m", getattr(snapshot, "candles", [])) or [])
        ok, _ = candle_quality(c5, sweep.MIN_CANDLES_REQUIRED, sweep.MIN_NONZERO_VOLUME_RATIO)
        if not ok:
            return "pending", None, {"reason": "watch_bad_5m_candle_quality", "watch_id": watch.watch_id}

        guard_status, guard_diag = self._watch_trend_guard(watch, snapshot)
        if guard_status != "ok":
            return guard_status, None, {"watch_id": watch.watch_id, **guard_diag}

        o, h, l, c, v = extract(c5)
        i = len(c) - 1
        atr5 = atr(h, l, c, 14)
        if atr5 <= 0:
            return "pending", None, {"reason": "invalid_atr", "watch_id": watch.watch_id, **guard_diag}
        atr_pct = float(atr5) / max(float(c[-1]), 1e-12)
        if not sweep.ATR_PCT_MIN <= atr_pct <= sweep.ATR_PCT_MAX:
            return "cancelled", None, {
                "reason": "atr_out_of_range", "watch_id": watch.watch_id,
                "atr_pct": atr_pct, **guard_diag,
            }
        level = float((watch.metadata or {}).get("liquidity_level") or 0.0)
        if level <= 0:
            return "cancelled", None, {"reason": "liquidity_level_missing", "watch_id": watch.watch_id}
        anchor_idx = self._watch_origin_index(watch, c5, "watch_candle_ts")
        if anchor_idx is None:
            return "cancelled", None, {"reason": "liquidity_watch_origin_missing", "watch_id": watch.watch_id, **guard_diag}
        bars_since_watch = i - anchor_idx
        if bars_since_watch > sweep.SWEEP_MAX_AGE_BARS:
            return "cancelled", None, {
                "reason": "sweep_watch_window_expired", "watch_id": watch.watch_id,
                "bars_since_watch": bars_since_watch, "max_watch_bars": sweep.SWEEP_MAX_AGE_BARS,
                **guard_diag,
            }

        progress_reason = "waiting_sweep"
        progress_diag: dict[str, Any] = {"liquidity_level": level, "bars_since_watch": bars_since_watch}
        best: ArmedSetup | None = None
        for sweep_idx in range(max(0, anchor_idx), i + 1):
            if watch.direction == Direction.LONG:
                extreme = float(l[sweep_idx])
                depth = (level - extreme) / atr5
                wick = sweep._lower_wick(o[sweep_idx], h[sweep_idx], l[sweep_idx], c[sweep_idx])
                sweep_close = sweep._close_pos(h[sweep_idx], l[sweep_idx], c[sweep_idx])
                recovered = float(c[sweep_idx]) >= level - atr5 * sweep.SWEEP_RECOVER_TOL_ATR and sweep_close >= 0.46
            else:
                extreme = float(h[sweep_idx])
                depth = (extreme - level) / atr5
                wick = sweep._upper_wick(o[sweep_idx], h[sweep_idx], l[sweep_idx], c[sweep_idx])
                sweep_close = sweep._close_pos(h[sweep_idx], l[sweep_idx], c[sweep_idx])
                recovered = float(c[sweep_idx]) <= level + atr5 * sweep.SWEEP_RECOVER_TOL_ATR and sweep_close <= 0.54
            rvol = relative_volume(v, sweep_idx, 24)
            if depth < sweep.SWEEP_MIN_DEPTH_ATR:
                continue
            progress_reason = "sweep_detected_waiting_reclaim"
            progress_diag = {
                "liquidity_level": level, "sweep_index": sweep_idx,
                "sweep_depth_atr": float(depth), "sweep_wick_ratio": float(wick),
                "sweep_rvol": float(rvol), "sweep_recovered": bool(recovered),
                "bars_since_watch": bars_since_watch,
            }
            if wick < sweep.SWEEP_MIN_WICK_RATIO:
                progress_reason = "sweep_detected_wick_insufficient"
                continue
            if rvol < sweep.SWEEP_MIN_RVOL:
                progress_reason = "sweep_detected_volume_insufficient"
                continue
            if not recovered:
                continue
            if sweep_idx < i:
                if watch.direction == Direction.LONG and min(l[sweep_idx + 1:i + 1]) < extreme - atr5 * sweep.RETEST_INVALIDATION_ATR:
                    progress_reason = "sweep_reclaim_invalidated"
                    continue
                if watch.direction == Direction.SHORT and max(h[sweep_idx + 1:i + 1]) > extreme + atr5 * sweep.RETEST_INVALIDATION_ATR:
                    progress_reason = "sweep_reclaim_invalidated"
                    continue

            if watch.direction == Direction.LONG:
                trigger = max(level + atr5 * 0.04, float(c[sweep_idx]) + atr5 * 0.03)
                stop = extreme - atr5 * sweep.SL_BUFFER_ATR
                entry_zone_low, entry_zone_high = trigger, level + atr5 * SWEEP_ENTRY_MAX_EXTENSION_ATR
                target_level = max(h[max(0, sweep_idx - sweep.TARGET_LOOKBACK):sweep_idx])
            else:
                trigger = min(level - atr5 * 0.04, float(c[sweep_idx]) - atr5 * 0.03)
                stop = extreme + atr5 * sweep.SL_BUFFER_ATR
                entry_zone_low, entry_zone_high = level - atr5 * SWEEP_ENTRY_MAX_EXTENSION_ATR, trigger
                target_level = min(l[max(0, sweep_idx - sweep.TARGET_LOOKBACK):sweep_idx])
            stop_atr = abs(trigger - stop) / atr5
            if stop_atr < ARM_MIN_STOP_ATR:
                progress_reason = "sweep_stop_too_tight"
                progress_diag["stop_atr"] = stop_atr
                continue
            if stop_atr > ARM_MAX_STOP_ATR:
                progress_reason = "sweep_stop_too_wide"
                progress_diag["stop_atr"] = stop_atr
                continue
            target, target_ratio = front_run_target(trigger, target_level, watch.direction)
            target_rr_cap = liquidity_sweep_target_rr()
            target, target_capped = cap_target_by_rr(trigger, stop, target, watch.direction, target_rr_cap)
            rr = abs(target - trigger) / max(abs(trigger - stop), 1e-12)
            if target <= 0 or rr < ARM_MIN_RR:
                progress_reason = "sweep_rr_too_low"
                progress_diag["structural_rr_estimate"] = rr
                continue
            age = i - sweep_idx
            depth_q = clamp((depth - sweep.SWEEP_MIN_DEPTH_ATR) / 0.65, 0, 1)
            wick_q = clamp((wick - sweep.SWEEP_MIN_WICK_RATIO) / 0.45, 0, 1)
            rv_q = clamp((rvol - 0.65) / 1.0, 0, 1)
            rr_q = clamp((rr - ARM_MIN_RR) / 1.6, 0, 1)
            fresh_q = clamp(1.0 - age / 4.0, 0, 1)
            score = round(58 + 42 * clamp(0.25 * depth_q + 0.20 * wick_q + 0.16 * rv_q + 0.25 * rr_q + 0.14 * fresh_q, 0, 1), 2)
            if score < SWEEP_ARM_MIN_SCORE:
                progress_reason = "sweep_quality_below_arm_threshold"
                progress_diag.update({"quality": score, "min_quality": SWEEP_ARM_MIN_SCORE})
                continue
            armed_at, expires_at = self._expiry(snapshot)
            lifecycle_id = str((watch.metadata or {}).get("lifecycle_id") or watch.watch_id)
            candidate = ArmedSetup(
                setup_id=f"{watch.symbol}:LS:{int(getattr(c5[sweep_idx], 'timestamp', armed_at))}:{watch.direction.value}",
                symbol=watch.symbol, strategy=Strategy.LIQUIDITY_SWEEP, direction=watch.direction,
                armed_at_ms=armed_at, expires_at_ms=expires_at,
                trigger_price=float(trigger), invalidation_price=float(stop), stop_price=float(stop),
                target_price=float(target), entry_zone_low=float(min(entry_zone_low, entry_zone_high)),
                entry_zone_high=float(max(entry_zone_low, entry_zone_high)), quality=score,
                risk_multiplier=max(float((watch.metadata or {}).get("risk_multiplier") or 1.0), 0.65),
                timeframe=watch.timeframe,
                reasons=("liquidity_sweep_5m", "level_recovered", "awaiting_micro_confirmation"),
                metadata={
                    "strategy_model": "stateful_liquidity_sweep_watch_v2",
                    "atr_value": float(atr5), "atr_pct": atr_pct, "sweep_level": level,
                    "sweep_extreme": float(extreme), "sweep_depth_atr": float(depth),
                    "sweep_wick_ratio": float(wick), "sweep_rvol": float(rvol),
                    "bars_since_sweep": int(age), "structural_target_price": float(target_level),
                    "target_front_run_ratio": float(target_ratio), "target_rr_cap": float(target_rr_cap),
                    "target_rr_capped": bool(target_capped), "structural_rr_estimate": float(rr),
                    "stop_atr_5m": float(stop_atr),
                    "regime": str((watch.metadata or {}).get("active_regime") or "TREND_CONTINUATION"),
                    "trend_aligned": True, "trend_direction_at_arm": watch.direction.value,
                    "trend_alignment_reason": "stateful_watch_current_5m_trend",
                    "trend_alignment_guard_version": 2,
                    "trend_revalidation_required": True, "trend_revalidation_guard_version": 2,
                    "lifecycle_id": lifecycle_id,
                    "lifecycle_origin": str((watch.metadata or {}).get("lifecycle_origin") or "liquidity_precursor"),
                    "origin_watch_id": watch.watch_id, "watch_created_at_ms": int(watch.created_at_ms),
                    "watch_promoted_at_ms": _now_ms(), "watch_candle_ts": int(getattr(c5[anchor_idx], "timestamp", 0) or 0),
                    **guard_diag,
                },
            )
            if best is None or candidate.quality > best.quality:
                best = candidate

        if best is not None:
            lifecycle_id = str((watch.metadata or {}).get("lifecycle_id") or watch.watch_id)
            return "armed", best, {
                "reason": "sweep_reclaimed", "watch_id": watch.watch_id,
                "lifecycle_id": lifecycle_id, "quality": best.quality, **guard_diag,
            }

        # If price has materially broken through the watched pool without reclaim,
        # the original level is no longer a valid trend-following sweep candidate.
        if watch.direction == Direction.LONG:
            failed_distance = (level - float(c[i])) / atr5
        else:
            failed_distance = (float(c[i]) - level) / atr5
        if failed_distance > 0.70:
            return "cancelled", None, {
                "reason": "sweep_failed_no_reclaim", "watch_id": watch.watch_id,
                "failed_close_distance_atr": failed_distance, **progress_diag, **guard_diag,
            }
        return "pending", None, {
            "reason": progress_reason, "watch_id": watch.watch_id, **progress_diag, **guard_diag,
        }

    def advance_watch(self, watch: SetupWatch, snapshot) -> tuple[str, ArmedSetup | None, dict]:
        now_ms = _now_ms()
        if now_ms >= int(watch.expires_at_ms):
            return "cancelled", None, {"reason": "watch_expired", "watch_id": watch.watch_id}
        frames = getattr(snapshot, "timeframes", {}) or {}
        if not frames.get("5m"):
            return "pending", None, {"reason": "watch_waiting_5m_data", "watch_id": watch.watch_id}
        if watch.strategy == Strategy.BREAKOUT_RETEST:
            return self._advance_breakout_watch(watch, snapshot)
        if watch.strategy == Strategy.LIQUIDITY_SWEEP:
            return self._advance_sweep_watch(watch, snapshot)
        return "cancelled", None, {"reason": "unsupported_watch_strategy", "watch_id": watch.watch_id}

    def discover(self, regime, snapshot, symbol: str, timeframe: str,
                 regime_metadata: dict | None = None) -> ArmedSetup | None:
        self.branch_trace = {}
        self.last_trace = {"accepted": False, "reason": "no_armable_setup", "branches": {}}
        if getattr(regime, "hard_block", False) and self._active_regime(regime_metadata) == "UNKNOWN":
            self.last_trace = {"accepted": False, "reason": "regime_unknown_hard_block"}
            return None
        active = self._active_regime(regime_metadata)
        candidates: list[ArmedSetup] = []
        # Strategy hierarchy: in TREND_CONTINUATION a valid BREAKOUT_RETEST has
        # first priority. LIQUIDITY_SWEEP is a secondary trend-following entry
        # only when no breakout/retest is armable. RANGE remains shadow-only.
        breakout_item = None
        if active == "TREND_CONTINUATION" or getattr(regime, "breakout_allowed", False):
            breakout_item = self._discover_breakout(regime, snapshot, symbol, timeframe, regime_metadata)
            if breakout_item:
                candidates.append(breakout_item)

        sweep_permitted = False
        if active == "RANGE":
            self.branch_trace["sweep"] = {"accepted": False, "reason": "regime_sweep_not_allowed"}
        elif active == "VOLATILE_SWEEP":
            sweep_permitted = bool(getattr(regime, "sweep_allowed", False))
            if not sweep_permitted:
                self.branch_trace["sweep"] = {"accepted": False, "reason": "regime_sweep_not_allowed"}
        elif active == "TREND_CONTINUATION":
            # Never let a sweep compete with a valid trend breakout. When no fresh
            # breakout/retest is armable, always *evaluate* the trend-aligned
            # liquidity branch. The sweep still has to pass its own structural
            # liquidity, reclaim, ATR, stop and RR gates, and _discover_sweep()
            # hard-blocks any direction that conflicts with the trend.
            sweep_permitted = breakout_item is None
            if breakout_item is not None:
                self.branch_trace["sweep"] = {"accepted": False, "reason": "primary_breakout_selected"}
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

        # Stateful v6.9 safety: a setup that was valid at arm time may not keep
        # its original direction forever. New v6.9 setups require a fresh closed
        # 5m trend context immediately before micro-confirmation. Missing context
        # waits; neutral/opposite context invalidates the stale directional setup.
        meta = dict(setup.metadata or {})
        if bool(meta.get("trend_revalidation_required")):
            current_direction, trend_diag = _snapshot_trend_direction(snapshot)
            if trend_diag.get("trend_context_status") != "ok":
                return "pending", None, {"reason": "trend_context_unavailable", **trend_diag}
            if current_direction is None:
                self._consume(setup, "trend_direction_lost", now_ms)
                return "cancelled", None, {
                    "reason": "trend_direction_lost",
                    "setup_direction": setup.direction.value,
                    **trend_diag,
                }
            if current_direction != setup.direction:
                self._consume(setup, "trend_direction_flipped", now_ms)
                return "cancelled", None, {
                    "reason": "trend_direction_flipped",
                    "setup_direction": setup.direction.value,
                    "current_trend_direction": current_direction.value,
                    **trend_diag,
                }
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
        if diag.get("confirmation_mode") == "recent_prearm_closed_1m":
            confirm_reason = "micro_confirmation_fast_1m"
        elif diag.get("confirmation_mode") == "postarm_closed_1m_strong_shape":
            confirm_reason = "micro_confirmation_1m_adaptive"
        else:
            confirm_reason = "micro_confirmation_1m"
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
