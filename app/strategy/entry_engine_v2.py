from __future__ import annotations

from hashlib import sha1
import time

from app.models.enums import Direction
from app.models.trading import ArmedSetup, SetupWatch, TradeIntent
from app.position.protection import (
    break_even_activation_ratio,
    break_even_buffer_bps,
    exit_fee_rate_estimate,
    profit_lock_activation_ratio,
    profit_lock_capture_ratio,
)
from app.strategy.v2_common import (
    StrategyCandidate, dynamic_min_rr, env_float, executable_price, orderbook_imbalance,
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _id(prefix: str, symbol: str, signature: str) -> str:
    digest = sha1(f"{symbol}|{signature}".encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


class EntryLifecycleV2:
    """Small, deterministic WATCH -> ARMED -> TRIGGERED lifecycle.

    It intentionally replaces the large legacy armed-entry decision tree while
    preserving the public persistence/recovery contract used by TradingOrchestrator.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = 600.0,
        watch_ttl_seconds: float = 1800.0,
        consumed_ttl_seconds: float = 3600.0,
        chase_tolerance_atr: float = 0.15,
        trigger_close_tolerance_atr: float = 0.08,
        fast_confirm_enabled: bool = True,
        fast_confirm_max_age_seconds: float = 30.0,
    ):
        self.ttl_ms = max(60_000, int(float(ttl_seconds) * 1000))
        self.watch_ttl_ms = max(300_000, int(float(watch_ttl_seconds) * 1000))
        self.consumed_ttl_ms = max(600_000, int(float(consumed_ttl_seconds) * 1000))
        self.chase_tolerance_atr = max(0.0, float(chase_tolerance_atr))
        self.trigger_close_tolerance_atr = max(0.0, float(trigger_close_tolerance_atr))
        self.fast_confirm_enabled = bool(fast_confirm_enabled)
        self.fast_confirm_max_age_ms = max(5_000, int(float(fast_confirm_max_age_seconds) * 1000))
        self._consumed: dict[str, tuple[int, str]] = {}
        self._consumed_watches: dict[str, tuple[int, str]] = {}

    def _purge(self, now_ms: int | None = None) -> None:
        now_ms = _now_ms() if now_ms is None else int(now_ms)
        self._consumed = {k: v for k, v in self._consumed.items() if int(v[0]) > now_ms}
        self._consumed_watches = {k: v for k, v in self._consumed_watches.items() if int(v[0]) > now_ms}

    def restore_consumed(self, setup_id: str, until_ms: int, reason: str = "restored") -> bool:
        if not setup_id or int(until_ms) <= _now_ms():
            return False
        self._consumed[str(setup_id)] = (int(until_ms), str(reason))
        return True

    def consumed_record(self, setup_id: str):
        self._purge()
        return self._consumed.get(str(setup_id))

    def _is_consumed(self, setup_id: str, now_ms: int | None = None):
        self._purge(now_ms)
        row = self._consumed.get(str(setup_id))
        return (row is not None, row[1] if row else None)

    def consume(self, setup: ArmedSetup, reason: str, now_ms: int | None = None) -> None:
        now_ms = _now_ms() if now_ms is None else int(now_ms)
        until = max(now_ms + self.consumed_ttl_ms, int(setup.expires_at_ms))
        self._consumed[str(setup.setup_id)] = (until, str(reason))

    def restore_consumed_watch(self, watch_id: str, until_ms: int, reason: str = "restored") -> bool:
        if not watch_id or int(until_ms) <= _now_ms():
            return False
        self._consumed_watches[str(watch_id)] = (int(until_ms), str(reason))
        return True

    def consumed_watch_record(self, watch_id: str):
        self._purge()
        return self._consumed_watches.get(str(watch_id))

    def consume_watch(self, watch: SetupWatch, reason: str) -> None:
        now_ms = _now_ms()
        until = max(now_ms + self.consumed_ttl_ms, int(watch.expires_at_ms))
        self._consumed_watches[str(watch.watch_id)] = (until, str(reason))

    def arm(self, candidate: StrategyCandidate, symbol: str, timeframe: str, *, watch: SetupWatch | None = None) -> ArmedSetup | None:
        now_ms = _now_ms()
        setup_id = _id("V2", symbol, candidate.signature)
        if self._is_consumed(setup_id, now_ms)[0]:
            return None
        metadata = dict(candidate.metadata or {})
        metadata.update({
            "engine_version": "v2",
            "candidate_signature": candidate.signature,
            "signal_entry_price": float(candidate.signal_entry),
            "structural_target_price": float(candidate.structural_target),
            "lifecycle_origin": "watch_promoted" if watch else "scanner_complete_setup",
        })
        if watch is not None:
            metadata["origin_watch_id"] = watch.watch_id
            metadata["lifecycle_id"] = (watch.metadata or {}).get("lifecycle_id") or watch.watch_id
        else:
            metadata["lifecycle_id"] = setup_id
        return ArmedSetup(
            setup_id=setup_id,
            symbol=str(symbol),
            strategy=candidate.strategy,
            direction=candidate.direction,
            armed_at_ms=now_ms,
            expires_at_ms=now_ms + self.ttl_ms,
            trigger_price=float(candidate.trigger_price),
            invalidation_price=float(candidate.invalidation_price),
            stop_price=float(candidate.stop_price),
            target_price=float(candidate.target_price),
            entry_zone_low=float(candidate.entry_zone_low),
            entry_zone_high=float(candidate.entry_zone_high),
            quality=float(candidate.quality),
            risk_multiplier=1.0,
            timeframe=str(timeframe or "5m"),
            reasons=tuple(candidate.reasons),
            metadata=metadata,
        )

    def watch(self, candidate: StrategyCandidate, symbol: str, timeframe: str) -> SetupWatch | None:
        now_ms = _now_ms()
        watch_id = _id("WATCHV2", symbol, candidate.signature)
        self._purge(now_ms)
        if watch_id in self._consumed_watches:
            return None
        metadata = dict(candidate.metadata or {})
        metadata.update({
            "engine_version": "v2",
            "candidate_signature": candidate.signature,
            "structure_ts": int(candidate.structure_ts),
            "trigger_price": float(candidate.trigger_price),
            "invalidation_price": float(candidate.invalidation_price),
            "stop_price": float(candidate.stop_price),
            "target_price": float(candidate.target_price),
            "structural_target_price": float(candidate.structural_target),
            "entry_zone_low": float(candidate.entry_zone_low),
            "entry_zone_high": float(candidate.entry_zone_high),
            "quality": float(candidate.quality),
            "lifecycle_id": watch_id,
            "lifecycle_origin": "v2_precursor",
        })
        return SetupWatch(
            watch_id=watch_id,
            symbol=str(symbol),
            strategy=candidate.strategy,
            direction=candidate.direction,
            created_at_ms=now_ms,
            expires_at_ms=now_ms + self.watch_ttl_ms,
            timeframe=str(timeframe or "5m"),
            reasons=tuple(candidate.reasons),
            metadata=metadata,
        )

    @staticmethod
    def watch_invalidated(watch: SetupWatch, snapshot) -> tuple[bool, str]:
        meta = dict(watch.metadata or {})
        invalidation = float(meta.get("invalidation_price") or 0.0)
        if invalidation <= 0:
            return False, ""
        try:
            candles = list(getattr(snapshot, "candles", []) or [])
            close = float(candles[-1].close) if candles else float(getattr(snapshot, "last", 0.0) or 0.0)
        except (TypeError, ValueError, AttributeError):
            return False, ""
        if watch.direction == Direction.LONG and close <= invalidation:
            return True, "watch_structure_invalidated"
        if watch.direction == Direction.SHORT and close >= invalidation:
            return True, "watch_structure_invalidated"
        return False, ""

    def trigger(self, setup: ArmedSetup, snapshot, decision_id: str) -> tuple[str, TradeIntent | None, dict]:
        now_ms = _now_ms()
        consumed, reason = self._is_consumed(setup.setup_id, now_ms)
        if consumed:
            return "cancelled", None, {"reason": "setup_already_consumed", "consumed_reason": reason}
        if now_ms >= int(setup.expires_at_ms):
            self.consume(setup, "setup_expired", now_ms)
            return "cancelled", None, {"reason": "setup_expired"}

        price = executable_price(snapshot, setup.direction)
        if price is None:
            return "pending", None, {"reason": "execution_quote_unavailable"}
        if not bool(getattr(snapshot, "orderbook_valid", True)):
            return "pending", None, {"reason": "orderbook_invalid"}

        if setup.direction == Direction.LONG and price <= float(setup.invalidation_price):
            self.consume(setup, "setup_invalidated", now_ms)
            return "cancelled", None, {"reason": "setup_invalidated", "price": price}
        if setup.direction == Direction.SHORT and price >= float(setup.invalidation_price):
            self.consume(setup, "setup_invalidated", now_ms)
            return "cancelled", None, {"reason": "setup_invalidated", "price": price}

        meta = dict(setup.metadata or {})
        atr_value = float(meta.get("atr_value") or 0.0)
        signal_entry = float(meta.get("signal_entry_price") or setup.trigger_price)
        if atr_value <= 0:
            self.consume(setup, "invalid_atr_metadata", now_ms)
            return "cancelled", None, {"reason": "invalid_atr_metadata"}

        stop = float(setup.stop_price)
        if (setup.direction == Direction.LONG and not stop < price) or (setup.direction == Direction.SHORT and not price < stop):
            self.consume(setup, "trigger_invalid_stop_geometry", now_ms)
            return "cancelled", None, {"reason": "trigger_invalid_stop_geometry", "price": price, "stop": stop}

        # Strategy V2 locks an absolute structure-derived target at ARM time.
        # Check it before the anti-chase gate so a setup that already completed
        # its move is terminal, while a merely extended setup can wait for re-entry.
        structural_target = float(meta.get("structural_target_price") or setup.target_price)
        target = float(setup.target_price)
        if target <= 0:
            self.consume(setup, "trigger_invalid_target_geometry", now_ms)
            return "cancelled", None, {"reason": "trigger_invalid_target_geometry", "price": price, "target": target}
        if setup.direction == Direction.LONG:
            if target <= price:
                self.consume(setup, "target_already_reached_before_entry", now_ms)
                return "cancelled", None, {
                    "reason": "target_already_reached_before_entry",
                    "price": price,
                    "target": target,
                    "structural_target": structural_target,
                }
        else:
            if target >= price:
                self.consume(setup, "target_already_reached_before_entry", now_ms)
                return "cancelled", None, {
                    "reason": "target_already_reached_before_entry",
                    "price": price,
                    "target": target,
                    "structural_target": structural_target,
                }

        # Anti-chase measures extension *beyond the strategy trigger*, not from
        # signal_entry.  In V2 the trigger can intentionally sit outside the raw
        # entry zone (for example a breakout continuation threshold), so using
        # signal_entry here made a valid trigger look chased before it could fire.
        trigger_anchor = float(setup.trigger_price)
        favorable_extension = (price - trigger_anchor) if setup.direction == Direction.LONG else (trigger_anchor - price)
        chase_limit = max(
            self.chase_tolerance_atr,
            env_float("V2_TRIGGER_MAX_CHASE_ATR", 0.35, 0.10, 1.50),
        ) * atr_value
        if favorable_extension > chase_limit:
            # Do NOT consume the setup. Extension is not invalidation. Keep it
            # ARMED so a pullback toward trigger geometry can still execute.
            return "pending", None, {
                "reason": "setup_extended_wait_reentry",
                "price": price,
                "signal_entry": signal_entry,
                "trigger_price": trigger_anchor,
                "extension_atr": favorable_extension / atr_value,
                "chase_limit_atr": chase_limit / atr_value,
                "entry_zone_low": float(setup.entry_zone_low),
                "entry_zone_high": float(setup.entry_zone_high),
            }

        imbalance = orderbook_imbalance(snapshot, 12)
        severe = env_float("V2_TRIGGER_ORDERBOOK_CONFLICT", 0.45, 0.15, 0.90)
        book_conflict = False
        if imbalance is not None:
            book_conflict = bool(
                (setup.direction == Direction.LONG and imbalance <= -severe)
                or (setup.direction == Direction.SHORT and imbalance >= severe)
            )

        # High-quality setups can confirm from the live executable quote + order
        # book instead of always waiting up to one full minute for a new closed
        # candle. This is deliberately strict and never bypasses target, stop,
        # invalidation, RR or anti-chase geometry.
        quote_received_ms = int(getattr(snapshot, "quote_received_ms", 0) or 0)
        quote_age_ms = (now_ms - quote_received_ms) if 0 < quote_received_ms <= now_ms else None
        live_quality_min = env_float("V2_LIVE_CONFIRM_MIN_QUALITY", 82.0, 70.0, 99.0)
        live_max_quote_age_ms = int(env_float("V2_LIVE_CONFIRM_MAX_QUOTE_AGE_MS", 2500.0, 250.0, 10000.0))
        live_trigger_tol = env_float("V2_LIVE_CONFIRM_TRIGGER_TOLERANCE_ATR", 0.03, 0.0, 0.20) * atr_value
        live_zone_tol = env_float("V2_LIVE_CONFIRM_ZONE_TOLERANCE_ATR", 0.20, 0.02, 0.60) * atr_value

        if setup.direction == Direction.LONG:
            trigger_reached = price >= trigger_anchor - live_trigger_tol
            geometry_low = float(setup.entry_zone_low)
            geometry_high = max(float(setup.entry_zone_high), trigger_anchor)
            zone_distance = max(0.0, price - geometry_high, geometry_low - price)
        else:
            trigger_reached = price <= trigger_anchor + live_trigger_tol
            geometry_low = min(float(setup.entry_zone_low), trigger_anchor)
            geometry_high = float(setup.entry_zone_high)
            zone_distance = max(0.0, geometry_low - price, price - geometry_high)

        live_confirmed = bool(
            self.fast_confirm_enabled
            and float(setup.quality) >= live_quality_min
            and quote_age_ms is not None
            and quote_age_ms <= live_max_quote_age_ms
            and imbalance is not None
            and not book_conflict
            and trigger_reached
            and zone_distance <= live_zone_tol
        )

        confirmation_mode = None
        confirmation_payload: dict = {}

        if live_confirmed:
            confirmation_mode = "live_microstructure"
            confirmation_payload = {
                "mode": confirmation_mode,
                "price": price,
                "trigger_price": float(setup.trigger_price),
                "quote_received_ms": quote_received_ms,
                "quote_age_ms": quote_age_ms,
                "orderbook_imbalance": imbalance,
                "zone_distance_atr": zone_distance / max(atr_value, 1e-12),
                "setup_quality": float(setup.quality),
            }
        else:
            frames = dict(getattr(snapshot, "timeframes", {}) or {})
            c1 = list(frames.get("1m") or [])
            if not c1:
                reason = "micro_orderbook_conflict" if book_conflict else "waiting_closed_1m_confirmation"
                trace = {"reason": reason}
                if imbalance is not None:
                    trace["orderbook_imbalance"] = imbalance
                return "pending", None, trace

            candle = c1[-1]
            cts = int(getattr(candle, "timestamp", 0) or 0)
            candle_close_ms = cts + 60_000
            age_from_arm = candle_close_ms - int(setup.armed_at_ms)
            recent_prearm = -self.fast_confirm_max_age_ms <= age_from_arm < 0
            post_arm = age_from_arm >= 0
            if not post_arm and not (self.fast_confirm_enabled and recent_prearm):
                return "pending", None, {
                    "reason": "waiting_post_arm_1m_close",
                    "age_from_arm_ms": age_from_arm,
                    "live_confirm_eligible": False,
                    "live_quote_age_ms": quote_age_ms,
                    "live_trigger_reached": trigger_reached,
                    "live_zone_distance_atr": zone_distance / max(atr_value, 1e-12),
                    "live_quality": float(setup.quality),
                }

            o, h, l, c = map(float, (candle.open, candle.high, candle.low, candle.close))
            rng = max(h - l, 1e-12)
            body_ratio = abs(c - o) / rng
            close_pos = (c - l) / rng
            tol = self.trigger_close_tolerance_atr * atr_value
            if setup.direction == Direction.LONG:
                candle_ok = c >= float(setup.trigger_price) - tol and c >= o and close_pos >= 0.52 and body_ratio >= 0.18
            else:
                candle_ok = c <= float(setup.trigger_price) + tol and c <= o and close_pos <= 0.48 and body_ratio >= 0.18
            if not candle_ok:
                return "pending", None, {
                    "reason": "micro_confirmation_pending",
                    "candle_close": c,
                    "trigger_price": setup.trigger_price,
                    "body_ratio": body_ratio,
                    "close_pos": close_pos,
                    "live_quote_age_ms": quote_age_ms,
                    "live_trigger_reached": trigger_reached,
                    "live_zone_distance_atr": zone_distance / max(atr_value, 1e-12),
                }

            if book_conflict:
                return "pending", None, {"reason": "micro_orderbook_conflict", "orderbook_imbalance": imbalance}

            confirmation_mode = "postarm_closed_1m" if post_arm else "recent_prearm_closed_1m"
            confirmation_payload = {
                "mode": confirmation_mode,
                "candle_ts": cts,
                "candle_close": c,
                "body_ratio": body_ratio,
                "close_pos": close_pos,
                "orderbook_imbalance": imbalance,
                "price": price,
            }

        # Structural target is immutable after ARM. RR is a viability check only.
        if setup.direction == Direction.LONG:
            reward = target - price
        else:
            reward = price - target
        risk = abs(price - stop)
        rr = reward / max(risk, 1e-12)
        min_rr = float(meta.get("minimum_viable_rr") or dynamic_min_rr(setup.strategy, setup.quality))
        if rr < min_rr:
            return "pending", None, {
                "reason": "trigger_rr_temporarily_low",
                "execution_rr": rr,
                "minimum_viable_rr": min_rr,
                "price": price,
                "stop": stop,
                "target": target,
                "structural_target": structural_target,
            }

        sl_pct = abs(price - stop) / price
        tp_pct = abs(target - price) / price
        metadata = {
            **meta,
            "engine_version": "v2",
            "entry_model": "v2_armed_micro_confirmed",
            "armed_setup_id": setup.setup_id,
            "armed_at_ms": setup.armed_at_ms,
            "triggered_at_ms": now_ms,
            "micro_confirmation": confirmation_payload,
            "signal_entry_price": signal_entry,
            "sl_pct": sl_pct,
            "tp_pct": tp_pct,
            "execution_rr": rr,
            "structural_target_price": structural_target,
            "minimum_viable_rr": min_rr,
            "target_locked_at_arm": True,
            "break_even_activation_ratio": break_even_activation_ratio(),
            "profit_lock_activation_ratio": profit_lock_activation_ratio(),
            "profit_lock_capture_ratio": profit_lock_capture_ratio(),
            "estimated_exit_fee_rate": exit_fee_rate_estimate(),
            "break_even_buffer_bps": break_even_buffer_bps(),
        }
        self.consume(setup, "setup_triggered", now_ms)
        intent = TradeIntent(
            decision_id=str(decision_id),
            symbol=setup.symbol,
            strategy=setup.strategy,
            direction=setup.direction,
            entry_price=float(price),
            stop_price=stop,
            target_price=float(target),
            quality=min(100.0, float(setup.quality) + 4.0),
            risk_multiplier=float(setup.risk_multiplier),
            timeframe=setup.timeframe,
            reasons=tuple(setup.reasons) + ("microstructure_confirmed",),
            metadata=metadata,
        )
        return "triggered", intent, {
            "reason": "microstructure_confirmed",
            "execution_rr": rr,
            "minimum_viable_rr": min_rr,
            "price": price,
            "orderbook_imbalance": imbalance,
            "confirmation_mode": confirmation_mode,
        }
