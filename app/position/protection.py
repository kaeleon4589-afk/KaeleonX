from __future__ import annotations

import math
import os
from typing import Any

from app.models.enums import Direction


DEFAULT_TARGET_FRONT_RUN_RATIO = 0.92
DEFAULT_BREAK_EVEN_ACTIVATION_RATIO = 0.55
DEFAULT_PROFIT_LOCK_ACTIVATION_RATIO = 0.80
DEFAULT_PROFIT_LOCK_CAPTURE_RATIO = 0.35
DEFAULT_EXIT_FEE_RATE_ESTIMATE = 0.0006
DEFAULT_BREAK_EVEN_BUFFER_BPS = 3.0


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = float(default)
    return min(high, max(low, value))


def target_front_run_ratio() -> float:
    return _env_float(
        "TRADE_TARGET_FRONT_RUN_RATIO",
        DEFAULT_TARGET_FRONT_RUN_RATIO,
        0.80,
        1.0,
    )


def break_even_activation_ratio() -> float:
    return _env_float(
        "TRADE_BREAK_EVEN_ACTIVATION_RATIO",
        DEFAULT_BREAK_EVEN_ACTIVATION_RATIO,
        0.20,
        0.90,
    )


def profit_lock_activation_ratio() -> float:
    return _env_float(
        "TRADE_PROFIT_LOCK_ACTIVATION_RATIO",
        DEFAULT_PROFIT_LOCK_ACTIVATION_RATIO,
        0.40,
        0.98,
    )


def profit_lock_capture_ratio() -> float:
    return _env_float(
        "TRADE_PROFIT_LOCK_CAPTURE_RATIO",
        DEFAULT_PROFIT_LOCK_CAPTURE_RATIO,
        0.05,
        0.80,
    )


def exit_fee_rate_estimate() -> float:
    return _env_float(
        "TRADE_EXIT_FEE_RATE_ESTIMATE",
        DEFAULT_EXIT_FEE_RATE_ESTIMATE,
        0.0,
        0.01,
    )


def break_even_buffer_bps() -> float:
    return _env_float(
        "TRADE_BREAK_EVEN_BUFFER_BPS",
        DEFAULT_BREAK_EVEN_BUFFER_BPS,
        0.0,
        50.0,
    )


def front_run_target(
    entry: float,
    structural_target: float,
    direction: Direction,
    ratio: float | None = None,
) -> tuple[float, float]:
    """Place the executable TP just before the structural liquidity target.

    The structural target remains useful for diagnostics, but sending the TP to
    the exact swing/liquidity level makes fills compete with the reversal zone.
    """
    entry = float(entry)
    structural_target = float(structural_target)
    used_ratio = float(target_front_run_ratio() if ratio is None else ratio)
    used_ratio = min(1.0, max(0.80, used_ratio))
    if entry <= 0 or structural_target <= 0:
        return structural_target, used_ratio
    if direction == Direction.LONG:
        if structural_target <= entry:
            return structural_target, used_ratio
        return entry + (structural_target - entry) * used_ratio, used_ratio
    if structural_target >= entry:
        return structural_target, used_ratio
    return entry - (entry - structural_target) * used_ratio, used_ratio


def calculate_break_even_price(
    position: Any,
    *,
    fallback_exit_fee_rate: float | None = None,
    buffer_bps: float | None = None,
) -> float | None:
    """Return a fee-aware break-even stop, including a small execution cushion."""
    try:
        entry = float(position.entry_price)
        qty = abs(float(position.quantity or position.remaining_quantity or 0.0))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(entry) or entry <= 0 or not math.isfinite(qty) or qty <= 0:
        return None

    fallback_rate = float(
        exit_fee_rate_estimate()
        if fallback_exit_fee_rate is None
        else fallback_exit_fee_rate
    )
    fallback_rate = min(0.01, max(0.0, fallback_rate))
    configured_rate = getattr(position, "estimated_exit_fee_rate", None)
    try:
        exit_rate = float(configured_rate) if configured_rate is not None else fallback_rate
    except (TypeError, ValueError):
        exit_rate = fallback_rate
    if not math.isfinite(exit_rate) or exit_rate < 0 or exit_rate >= 0.02:
        exit_rate = fallback_rate

    try:
        entry_fee = abs(float(getattr(position, "entry_fee", 0.0) or 0.0))
    except (TypeError, ValueError):
        entry_fee = 0.0
    if entry_fee <= 0:
        entry_fee = entry * qty * exit_rate

    cushion_bps = float(
        break_even_buffer_bps()
        if buffer_bps is None
        else buffer_bps
    )
    cushion = max(0.0, cushion_bps) / 10000.0

    if position.direction == Direction.LONG:
        denominator = 1.0 - exit_rate
        if denominator <= 0:
            return None
        price = (entry + entry_fee / qty) / denominator
        price *= 1.0 + cushion
    else:
        denominator = 1.0 + exit_rate
        price = (entry - entry_fee / qty) / denominator
        price *= max(0.0, 1.0 - cushion)

    return float(price) if math.isfinite(price) and price > 0 else None


def apply_intent_management(position: Any, intent: Any) -> Any:
    """Copy strategy exit-management metadata onto a filled Position."""
    metadata = getattr(intent, "metadata", {}) or {}
    position.initial_stop_price = float(
        getattr(position, "initial_stop_price", None) or position.stop_price
    )
    position.structural_target_price = float(
        metadata.get("structural_target_price")
        or getattr(position, "structural_target_price", None)
        or position.target_price
    )
    position.target_front_run_ratio = float(
        metadata.get("target_front_run_ratio")
        or getattr(position, "target_front_run_ratio", None)
        or target_front_run_ratio()
    )
    position.break_even_activation_ratio = float(
        metadata.get("break_even_activation_ratio")
        or getattr(position, "break_even_activation_ratio", None)
        or break_even_activation_ratio()
    )
    position.profit_lock_activation_ratio = float(
        metadata.get("profit_lock_activation_ratio")
        or getattr(position, "profit_lock_activation_ratio", None)
        or profit_lock_activation_ratio()
    )
    position.profit_lock_capture_ratio = float(
        metadata.get("profit_lock_capture_ratio")
        or getattr(position, "profit_lock_capture_ratio", None)
        or profit_lock_capture_ratio()
    )
    position.estimated_exit_fee_rate = float(
        metadata.get("estimated_exit_fee_rate")
        or getattr(position, "estimated_exit_fee_rate", None)
        or exit_fee_rate_estimate()
    )
    position.break_even_buffer_bps = float(
        metadata.get("break_even_buffer_bps")
        or getattr(position, "break_even_buffer_bps", None)
        or break_even_buffer_bps()
    )
    if not getattr(position, "management_stage", None):
        position.management_stage = "INITIAL"
    if getattr(position, "best_price", None) is None:
        position.best_price = float(position.entry_price)
    position.break_even_price = calculate_break_even_price(
        position,
        fallback_exit_fee_rate=position.estimated_exit_fee_rate,
        buffer_bps=position.break_even_buffer_bps,
    )
    return position
