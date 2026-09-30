from __future__ import annotations

import asyncio
from copy import copy
import inspect
import math
import time
from uuid import uuid4
from dataclasses import replace
from datetime import datetime, timezone, timedelta

from app.models.enums import Direction, Strategy
from app.strategy import breakout_retest as breakout
from app.models.trading import Position
from app.position.protection import apply_intent_management
from app.trading.persistence import TradePersistence
from app.trading.rejection_funnel import RejectionFunnel


# Both strategies require at least this RR before generating a signal. Enforce
# the same limit after executable bid/ask and DEMO slippage alter the entry.
MIN_EXECUTION_RR = 1.05

# Stable lifecycle stages make one setup traceable from the first precursor to
# the position that was ultimately opened (or the exact terminal rejection).
LIFECYCLE_STAGE_ORDER = {
    "WATCHING": 10,
    "ARMED": 20,
    "TRIGGERED": 30,
    "SIGNAL_ACCEPTED": 40,
    "RISK_APPROVED": 50,
    "ORDER_SUBMITTED": 60,
    "EXECUTION_PENDING": 65,
    "POSITION_OPENED": 70,
    "CANCELLED": 90,
    "REJECTED": 91,
    "ERROR": 99,
}
LIFECYCLE_PROGRESS_REPEAT_SECONDS = 60.0


class TradingOrchestrator:
    """Deterministic signal -> risk -> execution -> position pipeline.

    The critical rule is simple: after SIGNAL_ACCEPTED no diagnostic/database
    operation is allowed to sit between risk approval and order submission.
    Every accepted signal ends in a visible terminal state, and an actual fill is
    never re-labelled as a rejection just because downstream persistence failed.
    """

    def __init__(self, regime_engine, router, risk, execution, db, audit,
                 position_manager, signal_factory, cooldown_seconds=30,
                 execution_mode=None, on_position_opened=None, on_position_closed=None,
                 persistence=None, post_loss_global_cooldown_seconds=900.0,
                 post_loss_symbol_cooldown_seconds=1800.0,
                 entry_max_chase_atr=0.20, entry_max_adverse_reversal_atr=0.15,
                 entry_min_stop_atr=0.55, entry_min_stop_spreads=3.0,
                 entry_orderbook_conflict_threshold=0.35,
                 armed_entry_enabled=True, legacy_entry_fallback_enabled=False,
                 funnel_emit_seconds=300.0, funnel_emit_every=50, execution_quote_provider=None):
        self.execution_quote_provider = execution_quote_provider
        self.regime_engine = regime_engine
        self.router = router
        self.risk = risk
        self.execution = execution
        self.db = db
        self.audit = audit
        self.position_manager = position_manager
        self.signal_factory = signal_factory
        self.cooldown_seconds = cooldown_seconds
        self.last_decision = {}
        self.inflight = set()
        self.execution_mode = execution_mode or getattr(execution, "mode", "demo")
        self.on_position_opened = on_position_opened
        self.on_position_closed = on_position_closed
        self.persistence = persistence or TradePersistence(db, audit)
        self.pending_execution = None
        self.last_rejection = None
        self.entry_guard = None
        self.post_loss_global_cooldown_seconds = max(0.0, float(post_loss_global_cooldown_seconds))
        self.post_loss_symbol_cooldown_seconds = max(0.0, float(post_loss_symbol_cooldown_seconds))
        self.entry_max_chase_atr = max(0.0, float(entry_max_chase_atr))
        self.entry_max_adverse_reversal_atr = max(0.0, float(entry_max_adverse_reversal_atr))
        self.entry_min_stop_atr = max(0.0, float(entry_min_stop_atr))
        self.entry_min_stop_spreads = max(0.0, float(entry_min_stop_spreads))
        self.entry_orderbook_conflict_threshold = min(0.95, max(0.0, float(entry_orderbook_conflict_threshold)))
        self.armed_entry_enabled = bool(armed_entry_enabled)
        self.legacy_entry_fallback_enabled = bool(legacy_entry_fallback_enabled)
        self.armed_setups = {}
        self.watching_setups = {}
        self.rejection_funnel = RejectionFunnel(
            emit_seconds=funnel_emit_seconds, emit_every=funnel_emit_every
        )
        self.last_loss_at = 0.0
        self.last_symbol_loss_at = {}
        self._lifecycle_progress = {}

    async def _refresh_entry_quote(self, snapshot, decision_id, user_id):
        """Refresh inside the consumer, after queue/DB waits; never retimestamp old data."""
        if self.execution_quote_provider is None:
            return snapshot  # Compatibility for offline/replay callers; final age guard still applies.
        previous_ms = getattr(snapshot, "quote_received_ms", None)
        quote = await asyncio.wait_for(self.execution_quote_provider(snapshot.symbol), timeout=8.0)
        bid, ask = float(quote.bid), float(quote.ask)
        received_ms = int(quote.quote_received_ms)
        age_ms = int(time.time() * 1000) - received_ms
        if (getattr(quote, "symbol", None) != snapshot.symbol
                or not math.isfinite(bid) or not math.isfinite(ask)
                or not 0 < bid <= ask or not 0 <= age_ms <= 2_000
                or not getattr(quote, "orderbook_valid", False)):
            raise ValueError("invalid_or_stale_execution_quote")
        fresh = copy(snapshot)  # Snapshot is shared by users; preserve candles and monitor flags.
        for field in ("bid", "ask", "bids", "asks", "last", "quote_received_ms", "orderbook_valid"):
            setattr(fresh, field, getattr(quote, field, None))
        self.audit.event(
            "EXECUTION_QUOTE_REFRESHED", decision_id, user_id=user_id, mode=self.execution_mode,
            symbol=snapshot.symbol, previous_quote_received_ms=previous_ms,
            quote_received_ms=received_ms, quote_age_ms=age_ms, bid=bid, ask=ask,
        )
        return fresh

    @staticmethod
    def _enum_value(value):
        return getattr(value, "value", str(value) if value is not None else None)

    @staticmethod
    def _metadata_of(item) -> dict:
        raw = getattr(item, "metadata", {}) if item is not None else {}
        return dict(raw or {}) if isinstance(raw, dict) else {}

    def _lifecycle_context(self, *, watch=None, setup=None, intent=None, decision_id=None) -> dict:
        item = intent or setup or watch
        meta = self._metadata_of(item)
        watch_id = str(
            meta.get("origin_watch_id")
            or (getattr(watch, "watch_id", None) if watch is not None else "")
            or ""
        ) or None
        setup_id = str(
            meta.get("armed_setup_id")
            or (getattr(setup, "setup_id", None) if setup is not None else "")
            or ""
        ) or None
        lifecycle_id = str(
            meta.get("lifecycle_id")
            or watch_id
            or setup_id
            or decision_id
            or ""
        ) or None
        return {
            "lifecycle_id": lifecycle_id,
            "watch_id": watch_id,
            "setup_id": setup_id,
        }

    def _ensure_armed_lifecycle(self, setup, *, watch=None):
        if setup is None:
            return None
        meta = self._metadata_of(setup)
        if watch is not None:
            watch_meta = self._metadata_of(watch)
            lifecycle_id = str(watch_meta.get("lifecycle_id") or getattr(watch, "watch_id", None) or setup.setup_id)
            meta.setdefault("origin_watch_id", getattr(watch, "watch_id", None))
            meta.setdefault("watch_created_at_ms", getattr(watch, "created_at_ms", None))
            meta.setdefault("lifecycle_origin", watch_meta.get("lifecycle_origin") or "watched_precursor")
        else:
            lifecycle_id = str(meta.get("lifecycle_id") or setup.setup_id)
            meta.setdefault("lifecycle_origin", "direct_complete_setup")
        meta["lifecycle_id"] = lifecycle_id
        if meta == self._metadata_of(setup):
            return setup
        return replace(setup, metadata=meta)

    @staticmethod
    def _lifecycle_stage_field(stage: str) -> str:
        return f"{str(stage).strip().lower()}_at_ms"

    def _schedule_lifecycle_stage(
        self, user_id, *, lifecycle_id, stage, symbol, strategy=None, direction=None,
        decision_id=None, watch_id=None, setup_id=None, position_id=None, reason=None,
        terminal=False, details=None,
    ) -> None:
        if not lifecycle_id:
            return
        now_ms = int(time.time() * 1000)
        stage = str(stage or "UNKNOWN").upper()
        document = {
            "user_id": user_id,
            "mode": self.execution_mode,
            "lifecycle_id": str(lifecycle_id),
            "symbol": symbol,
            "current_stage": stage,
            "stage_order": int(LIFECYCLE_STAGE_ORDER.get(stage, 0)),
            "last_event_at_ms": now_ms,
            "terminal": bool(terminal),
        }
        document[self._lifecycle_stage_field(stage)] = now_ms
        if strategy is not None:
            document["strategy"] = self._enum_value(strategy)
        if direction is not None:
            document["direction"] = self._enum_value(direction)
        if decision_id:
            document["last_decision_id"] = str(decision_id)
        if watch_id:
            document["watch_id"] = str(watch_id)
        if setup_id:
            document["setup_id"] = str(setup_id)
        if position_id:
            document["position_id"] = str(position_id)
        if reason:
            document["last_reason"] = str(reason)
        if terminal:
            document["terminal_reason"] = str(reason or stage.lower())
            document["terminal_at_ms"] = now_ms
        if isinstance(details, dict) and details:
            document["last_details"] = dict(details)
        self.persistence.schedule_upsert(
            collection="setup_lifecycles",
            key={"user_id": user_id, "mode": self.execution_mode, "lifecycle_id": str(lifecycle_id)},
            document=document,
            event_name="LIFECYCLE_STATE_PERSIST_ERROR",
            decision_id=decision_id or str(lifecycle_id),
            user_id=user_id, mode=self.execution_mode, symbol=symbol,
        )
        # Keep an immutable-ish event ledger as the authoritative journey history.
        # Summary upserts above are convenient for "where is it now?", while these
        # stage rows remain correct even if asynchronous Mongo writes complete out of order.
        event_id = f"{user_id}:{self.execution_mode}:{lifecycle_id}:{stage}:{decision_id or now_ms}"
        event_document = {
            **document,
            "event_id": event_id,
            "stage_at_ms": now_ms,
        }
        self.persistence.schedule_upsert(
            collection="setup_lifecycle_events",
            key={"event_id": event_id},
            document=event_document,
            event_name="LIFECYCLE_STATE_PERSIST_ERROR",
            decision_id=decision_id or str(lifecycle_id),
            user_id=user_id, mode=self.execution_mode, symbol=symbol,
        )

    def _emit_lifecycle_progress(
        self, event_name, *, phase, decision_id, user_id, symbol, lifecycle_ctx,
        strategy=None, direction=None, reason=None, trace=None, force=False,
    ) -> bool:
        lifecycle_id = (lifecycle_ctx or {}).get("lifecycle_id")
        if not lifecycle_id:
            return False
        phase = str(phase).upper()
        reason = str(reason or "pending")
        key = (str(lifecycle_id), phase)
        now = time.monotonic()
        previous = self._lifecycle_progress.get(key)
        if (not force and previous and previous[0] == reason
                and now - previous[1] < LIFECYCLE_PROGRESS_REPEAT_SECONDS):
            return False
        self._lifecycle_progress[key] = (reason, now)
        self.audit.event(
            event_name, decision_id, user_id=user_id, mode=self.execution_mode, symbol=symbol,
            lifecycle_id=lifecycle_id, watch_id=(lifecycle_ctx or {}).get("watch_id"),
            setup_id=(lifecycle_ctx or {}).get("setup_id"), stage=phase,
            stage_order=LIFECYCLE_STAGE_ORDER.get(phase), strategy=self._enum_value(strategy),
            direction=self._enum_value(direction), reason=reason, trace=trace or {},
        )
        self._schedule_lifecycle_stage(
            user_id, lifecycle_id=lifecycle_id, stage=phase, symbol=symbol,
            strategy=strategy, direction=direction, decision_id=decision_id,
            watch_id=(lifecycle_ctx or {}).get("watch_id"),
            setup_id=(lifecycle_ctx or {}).get("setup_id"),
            reason=reason, details={"progress_reason": reason},
        )
        return True

    def _terminal_lifecycle(
        self, user_id, *, lifecycle_ctx, symbol, decision_id, reason,
        strategy=None, direction=None, stage="REJECTED", details=None,
    ) -> None:
        lifecycle_id = (lifecycle_ctx or {}).get("lifecycle_id")
        if not lifecycle_id:
            return
        self._schedule_lifecycle_stage(
            user_id, lifecycle_id=lifecycle_id, stage=stage, symbol=symbol,
            strategy=strategy, direction=direction, decision_id=decision_id,
            watch_id=(lifecycle_ctx or {}).get("watch_id"),
            setup_id=(lifecycle_ctx or {}).get("setup_id"),
            reason=reason, terminal=True, details=details,
        )

    @staticmethod
    def _epoch_seconds(value):
        if value in (None, ''):
            return 0.0
        if isinstance(value, (int, float)):
            raw = float(value)
            return raw / 1000.0 if raw > 100_000_000_000 else raw
        try:
            text = str(value).replace('Z', '+00:00')
            return datetime.fromisoformat(text).timestamp()
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _position_net_result(position):
        try:
            explicit = getattr(position, 'net_pnl', None)
            if explicit is not None:
                return float(explicit)
            return (float(getattr(position, 'realized_pnl', 0.0) or 0.0)
                    - float(getattr(position, 'entry_fee', 0.0) or 0.0)
                    - float(getattr(position, 'exit_fee', 0.0) or 0.0)
                    + float(getattr(position, 'funding_pnl', 0.0) or 0.0))
        except (TypeError, ValueError):
            return 0.0

    def register_closed_position(self, position):
        if str(getattr(position, 'status', '')).upper() != 'CLOSED':
            return
        net = self._position_net_result(position)
        if not math.isfinite(net) or net >= 0:
            return
        closed_at = self._epoch_seconds(getattr(position, 'closed_at', None)) or time.time()
        symbol = str(getattr(position, 'symbol', '') or '')
        self.last_loss_at = max(self.last_loss_at, closed_at)
        if symbol:
            self.last_symbol_loss_at[symbol] = max(self.last_symbol_loss_at.get(symbol, 0.0), closed_at)
        if self.audit:
            self.audit.event(
                'POST_LOSS_COOLDOWN_STARTED', getattr(position, 'decision_id', 'loss'),
                user_id=self.position_manager.owner_user_id, mode=self.execution_mode, symbol=symbol or None,
                net_pnl=round(net, 8), global_seconds=self.post_loss_global_cooldown_seconds,
                symbol_seconds=self.post_loss_symbol_cooldown_seconds,
            )

    async def _persist_armed_active(self, user_id, setup) -> None:
        """Make ARMED durable before returning control to the scanner.

        Unlike diagnostic writes this is recovery-critical state. Awaiting the first
        bounded Mongo write also prevents an older delayed ACTIVE write from racing
        a later terminal/cancel write for the same setup.
        """
        expires_at = datetime.fromtimestamp(int(setup.expires_at_ms) / 1000.0, tz=timezone.utc)
        ok, err = await self.persistence._upsert(
            "armed_setups",
            {"user_id": user_id, "mode": self.execution_mode, "symbol": setup.symbol},
            {
                "user_id": user_id, "mode": self.execution_mode, "symbol": setup.symbol,
                "setup_id": setup.setup_id, "active": True, "setup": setup,
                "armed_at_ms": int(setup.armed_at_ms), "expires_at_ms": int(setup.expires_at_ms),
                "expires_at": expires_at, "terminal_reason": None,
            },
        )
        if not ok:
            self.audit.event(
                "ARMED_STATE_PERSIST_ERROR", setup.setup_id, level="ERROR",
                user_id=user_id, mode=self.execution_mode, symbol=setup.symbol, error=err,
            )

    async def _persist_watch_active(self, user_id, watch) -> None:
        expires_at = datetime.fromtimestamp(int(watch.expires_at_ms) / 1000.0, tz=timezone.utc)
        ok, err = await self.persistence._upsert(
            "setup_watches",
            {"user_id": user_id, "mode": self.execution_mode, "symbol": watch.symbol},
            {
                "user_id": user_id, "mode": self.execution_mode, "symbol": watch.symbol,
                "watch_id": watch.watch_id, "active": True, "watch": watch,
                "created_at_ms": int(watch.created_at_ms), "expires_at_ms": int(watch.expires_at_ms),
                "expires_at": expires_at, "terminal_reason": None,
            },
        )
        if not ok:
            self.audit.event(
                "WATCH_STATE_PERSIST_ERROR", watch.watch_id, level="ERROR",
                user_id=user_id, mode=self.execution_mode, symbol=watch.symbol, error=err,
            )

    def _schedule_watch_terminal(self, user_id, watch, reason: str) -> None:
        now_ms = int(time.time() * 1000)
        expires_at = datetime.fromtimestamp(max(int(watch.expires_at_ms), now_ms + 60_000) / 1000.0, tz=timezone.utc)
        self.persistence.schedule_upsert(
            collection="setup_watches",
            key={"user_id": user_id, "mode": self.execution_mode, "symbol": watch.symbol},
            document={
                "user_id": user_id, "mode": self.execution_mode, "symbol": watch.symbol,
                "watch_id": watch.watch_id, "active": False, "watch": watch,
                "terminal_reason": str(reason), "terminal_at_ms": now_ms,
                "expires_at_ms": int(watch.expires_at_ms), "expires_at": expires_at,
            },
            event_name="WATCH_STATE_PERSIST_ERROR", decision_id=watch.watch_id,
            user_id=user_id, mode=self.execution_mode, symbol=watch.symbol,
        )

    def _schedule_armed_terminal(self, user_id, setup, reason: str) -> None:
        now_ms = int(time.time() * 1000)
        record = None
        armed_engine = getattr(self.router, "armed", None)
        if armed_engine is not None and hasattr(armed_engine, "consumed_record"):
            record = armed_engine.consumed_record(setup.setup_id)
        until_ms = int(record[0]) if record else max(int(setup.expires_at_ms), now_ms + 3_600_000)
        consumed_reason = str(record[1]) if record else str(reason)
        expires_at = datetime.fromtimestamp(until_ms / 1000.0, tz=timezone.utc)
        self.persistence.schedule_upsert(
            collection="armed_setups",
            key={"user_id": user_id, "mode": self.execution_mode, "symbol": setup.symbol},
            document={
                "user_id": user_id, "mode": self.execution_mode, "symbol": setup.symbol,
                "setup_id": setup.setup_id, "active": False, "setup": setup,
                "terminal_reason": str(reason), "terminal_at_ms": now_ms,
                "expires_at_ms": until_ms, "expires_at": expires_at,
            },
            event_name="ARMED_STATE_PERSIST_ERROR", decision_id=setup.setup_id,
            user_id=user_id, mode=self.execution_mode, symbol=setup.symbol,
        )
        self.persistence.schedule_upsert(
            collection="armed_consumed_setups",
            key={"user_id": user_id, "mode": self.execution_mode, "setup_id": setup.setup_id},
            document={
                "user_id": user_id, "mode": self.execution_mode, "setup_id": setup.setup_id,
                "symbol": setup.symbol, "reason": consumed_reason, "until_ms": until_ms,
                "expires_at": expires_at,
            },
            event_name="ARMED_CONSUMED_PERSIST_ERROR", decision_id=setup.setup_id,
            user_id=user_id, mode=self.execution_mode, symbol=setup.symbol,
        )

    def _funnel(self, stage, reason=None, *, user_id=None, symbol=None):
        self.rejection_funnel.record(stage, reason)
        if self.rejection_funnel.should_emit():
            self.audit.event(
                'REJECTION_FUNNEL', 'system', user_id=user_id,
                mode=self.execution_mode, symbol=symbol,
                **self.rejection_funnel.payload(),
            )
            self.rejection_funnel.mark_emitted()

    def seed_loss_cooldowns(self, rows):
        now = time.time()
        horizon = max(self.post_loss_global_cooldown_seconds, self.post_loss_symbol_cooldown_seconds)
        if horizon <= 0:
            return
        for row in rows or []:
            if str(row.get('status', '')).upper() != 'CLOSED':
                continue
            closed_at = self._epoch_seconds(row.get('closed_at'))
            if not closed_at or now - closed_at > horizon:
                continue
            try:
                explicit = row.get('net_pnl')
                net = (float(explicit) if explicit is not None else
                       float(row.get('realized_pnl') or 0.0)
                       - float(row.get('entry_fee') or 0.0)
                       - float(row.get('exit_fee') or 0.0)
                       + float(row.get('funding_pnl') or 0.0))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(net) or net >= 0:
                continue
            symbol = str(row.get('symbol') or '')
            self.last_loss_at = max(self.last_loss_at, closed_at)
            if symbol:
                self.last_symbol_loss_at[symbol] = max(self.last_symbol_loss_at.get(symbol, 0.0), closed_at)

    def _has_open_position(self, symbol=None, user_id=None):
        # Source-bot invariant: one open trade at a time per user/runtime.
        # The old expression used ``user_id is not None or p.symbol == symbol``;
        # because user_id is always present in production it was easy to misread
        # and impossible to reason about. Keep the intended invariant explicit.
        return any(p.status == "OPEN" for p in self.position_manager.positions.values())

    @staticmethod
    def _position_from_result(raw, intent) -> Position:
        if isinstance(raw, Position):
            return apply_intent_management(raw, intent)
        direction = raw.get("direction", intent.direction)
        if not isinstance(direction, Direction):
            direction = Direction(str(direction))
        position = Position(
            position_id=str(raw["position_id"]),
            decision_id=str(raw.get("decision_id") or intent.decision_id),
            symbol=str(raw.get("symbol") or intent.symbol),
            direction=direction,
            quantity=float(raw.get("quantity") or 0),
            entry_price=float(raw.get("entry_price") or intent.entry_price),
            stop_price=float(raw.get("stop_price") or intent.stop_price),
            target_price=float(raw.get("target_price") or intent.target_price),
            status=str(raw.get("status", "OPEN")),
            realized_pnl=float(raw.get("realized_pnl", 0) or 0),
            unrealized_pnl=float(raw.get("unrealized_pnl", 0) or 0),
            tp1_price=raw.get("tp1_price"),
            tp2_price=raw.get("tp2_price"),
            remaining_quantity=raw.get("remaining_quantity"),
            tp1_hit=bool(raw.get("tp1_hit", False)),
            stop_moved_to_breakeven=bool(raw.get("stop_moved_to_breakeven", False)),
            entry_fee=float(raw.get("entry_fee", 0) or 0),
            exit_fee=float(raw.get("exit_fee", 0) or 0),
            funding_pnl=float(raw.get("funding_pnl", 0) or 0),
            initial_stop_price=raw.get("initial_stop_price"),
            structural_target_price=raw.get("structural_target_price"),
            target_front_run_ratio=raw.get("target_front_run_ratio"),
            break_even_price=raw.get("break_even_price"),
            break_even_activation_ratio=float(raw.get("break_even_activation_ratio", 0.55) or 0.55),
            profit_lock_activation_ratio=float(raw.get("profit_lock_activation_ratio", 0.80) or 0.80),
            profit_lock_capture_ratio=float(raw.get("profit_lock_capture_ratio", 0.35) or 0.35),
            profit_lock_price=raw.get("profit_lock_price"),
            management_stage=str(raw.get("management_stage", "INITIAL") or "INITIAL"),
            best_price=raw.get("best_price"),
            estimated_exit_fee_rate=float(raw.get("estimated_exit_fee_rate", 0.0006) or 0.0006),
            break_even_buffer_bps=float(raw.get("break_even_buffer_bps", 3.0) or 3.0),
            protection_update_pending=bool(raw.get("protection_update_pending", False)),
            opened_at=raw.get("opened_at"),
            closed_at=raw.get("closed_at"),
            exit_price=raw.get("exit_price"),
            exit_reason=raw.get("exit_reason"),
        )
        return apply_intent_management(position, intent)

    def _decision_doc(self, snapshot, intent, risk, *, status, execution_rr,
                      structural_rr, execution=None):
        lifecycle_ctx = self._lifecycle_context(intent=intent, decision_id=getattr(intent, "decision_id", None))
        doc = {
            "symbol": str(snapshot.symbol),
            "timeframe": str(snapshot.timeframe),
            "lifecycle_id": lifecycle_ctx.get("lifecycle_id"),
            "watch_id": lifecycle_ctx.get("watch_id"),
            "setup_id": lifecycle_ctx.get("setup_id"),
            "strategy": getattr(intent.strategy, "value", str(intent.strategy)),
            "direction": getattr(intent.direction, "value", str(intent.direction)),
            "quality": float(intent.quality),
            "entry_price": float(intent.entry_price),
            "stop_price": float(intent.stop_price),
            "target_price": float(intent.target_price),
            "execution_rr": float(execution_rr),
            "structural_rr": float(structural_rr) if structural_rr is not None else None,
            "risk_approved": bool(getattr(risk, "approved", False)),
            "risk_reason": str(getattr(risk, "reason", "")),
            "quote_notional": float(getattr(risk, "quantity", 0) or 0),
            "base_quantity": float(getattr(risk, "base_quantity", 0) or 0),
            "margin_required": float(getattr(risk, "margin_required", 0) or 0),
            "leverage": int(getattr(risk, "leverage", 1) or 1),
            "status": status,
        }
        if execution is not None:
            doc["execution"] = {
                k: v for k, v in execution.items()
                if k not in {"position", "exchange_response", "order_state"}
            }
        return doc

    async def _notify_opened(self, position, decision_id, user_id):
        if not self.on_position_opened:
            return
        try:
            result = self.on_position_opened(position)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            self.audit.event(
                "TELEGRAM_NOTIFY_FAILED", decision_id, level="ERROR",
                user_id=user_id, mode=self.execution_mode, symbol=position.symbol,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _notify_closed(self, position, decision_id, user_id):
        if not self.on_position_closed:
            return
        try:
            result = self.on_position_closed(position)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            self.audit.event(
                "TELEGRAM_NOTIFY_FAILED", decision_id, level="ERROR",
                user_id=user_id, mode=self.execution_mode, symbol=position.symbol,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def on_snapshot(self, snapshot, equity, user_id=None, allow_entries=True,
                          available_equity=None):
        now_ms = int(time.time() * 1000)

        # LIVE reconciliation runs even while new entries are paused. This is what
        # lets a delayed exchange fill or externally closed position converge back
        # into KAELEON state.
        if hasattr(self.execution, "sync_symbol"):
            try:
                exchange_positions = await self.execution.sync_symbol(snapshot.symbol)
                settlements = None
                if hasattr(self.execution, 'settlements'):
                    remote_ids = {str(r.get('id') or r.get('openId')) for r in exchange_positions}
                    missing = [p.position_id for p in self.position_manager.positions.values()
                               if p.status == 'OPEN' and p.symbol == snapshot.symbol and p.position_id not in remote_ids]
                    settlements = await self.execution.settlements(snapshot.symbol, missing) if missing else {}
                changes = self.position_manager.reconcile_exchange(
                    exchange_positions, now_ms, symbol=snapshot.symbol,
                    persist=False, notify=False, settlements=settlements,
                )
                if hasattr(self.execution, 'ensure_protection'):
                    for held in self.position_manager.positions.values():
                        needs_sync = (not held.protected) or bool(getattr(held, 'protection_update_pending', False))
                        if held.status != 'OPEN' or held.symbol != snapshot.symbol or not needs_sync:
                            continue
                        try:
                            await self.execution.ensure_protection(held)
                            held.protected = True
                            held.protection_update_pending = False
                            held.revision += 1
                            changes['updated'].append(held)
                            self.audit.event(
                                'POSITION_PROTECTION_SYNCED', held.decision_id,
                                user_id=user_id, mode=self.execution_mode, symbol=held.symbol,
                                position_id=held.position_id, stop_price=held.stop_price,
                                target_price=held.target_price,
                                management_stage=getattr(held, 'management_stage', 'INITIAL'),
                            )
                        except Exception as exc:
                            held.protection_update_pending = True
                            self.audit.event(
                                'POSITION_PROTECTION_SYNC_ERROR', held.decision_id, level='ERROR',
                                user_id=user_id, mode=self.execution_mode, symbol=held.symbol,
                                position_id=held.position_id, stop_price=held.stop_price,
                                target_price=held.target_price,
                                management_stage=getattr(held, 'management_stage', 'INITIAL'),
                                error=f"{type(exc).__name__}: {exc}",
                            )
                for position in changes.get("updated", []):
                    self.persistence.schedule_position_save(
                        position=position, user_id=user_id, mode=self.execution_mode,
                        symbol=position.symbol,
                    )
                for position in changes.get("closed", []):
                    self.register_closed_position(position)
                    self.persistence.schedule_position_save(
                        position=position, user_id=user_id, mode=self.execution_mode,
                        symbol=position.symbol,
                    )
                    await self._notify_closed(position, position.decision_id, user_id)
                for position in changes.get("opened", []):
                    pending = self.pending_execution
                    if pending and (
                        position.decision_id == pending.get("decision_id")
                        or position.symbol == pending.get("symbol")
                    ):
                        intent = pending.get("intent")
                        if intent is not None:
                            apply_intent_management(position, intent)
                            position.strategy = getattr(intent.strategy, "value", str(intent.strategy))
                            position.quality = round(float(intent.quality), 2)
                            position.execution_rr = pending.get("execution_rr")
                            if pending.get("structural_rr") is not None:
                                position.structural_rr = pending.get("structural_rr")
                        ok, _ = await self.persistence.save_position(position=position, user_id=user_id, mode=self.execution_mode)
                        if ok:
                            await self._clear_pending(user_id)
                    self.persistence.schedule_position_save(
                        position=position, user_id=user_id, mode=self.execution_mode,
                        symbol=position.symbol,
                    )
                    await self._notify_opened(position, position.decision_id, user_id)
            except Exception as exc:
                allow_entries = False
                self.last_rejection = 'position_sync_failed'
                self.audit.event(
                    "POSITION_SYNC_ERROR", user_id, user_id=user_id,
                    mode=self.execution_mode, symbol=snapshot.symbol,
                    error=f"{type(exc).__name__}: {exc}",
                )

        if snapshot.last:
            price = snapshot.last if isinstance(snapshot.last, (int, float)) else snapshot.last.close
            protection_changes = self.position_manager.mark(
                snapshot.symbol, price, now_ms,
                bid=getattr(snapshot, "bid", None),
                ask=getattr(snapshot, "ask", None),
                quote_received_ms=getattr(snapshot, "quote_received_ms", None),
            )
            if protection_changes and hasattr(self.execution, 'ensure_protection'):
                for position in protection_changes:
                    if position.status != 'OPEN':
                        continue
                    try:
                        await self.execution.ensure_protection(position)
                        position.protected = True
                        position.protection_update_pending = False
                        position.revision += 1
                        self.persistence.schedule_position_save(
                            position=position, user_id=user_id, mode=self.execution_mode,
                            symbol=position.symbol,
                        )
                        self.audit.event(
                            'POSITION_PROTECTION_SYNCED', position.decision_id,
                            user_id=user_id, mode=self.execution_mode, symbol=position.symbol,
                            position_id=position.position_id, stop_price=position.stop_price,
                            target_price=position.target_price,
                            management_stage=getattr(position, 'management_stage', 'INITIAL'),
                        )
                    except Exception as exc:
                        position.protection_update_pending = True
                        self.persistence.schedule_position_save(
                            position=position, user_id=user_id, mode=self.execution_mode,
                            symbol=position.symbol,
                        )
                        self.audit.event(
                            'POSITION_PROTECTION_SYNC_ERROR', position.decision_id, level='ERROR',
                            user_id=user_id, mode=self.execution_mode, symbol=position.symbol,
                            position_id=position.position_id, stop_price=position.stop_price,
                            target_price=position.target_price,
                            management_stage=getattr(position, 'management_stage', 'INITIAL'),
                            error=f"{type(exc).__name__}: {exc}",
                        )

        # An exchange-accepted but not-yet-confirmed order reserves the runtime.
        # Without this lock the scanner could submit a second symbol while CoinW
        # is still making the first position visible.
        if self.pending_execution and not self._has_open_position(user_id=user_id):
            pending = self.pending_execution
            if snapshot.symbol == pending.get("symbol") and hasattr(self.execution, "pending_order_status"):
                status_info = await self.execution.pending_order_status(pending.get("order_id"))
                order_status = str((status_info or {}).get("status") or "unknown").lower()
                age = time.monotonic() - float(pending.get("created_monotonic") or time.monotonic())
                if order_status == "cancel":
                    self.audit.event(
                        "EXECUTION_REJECTED", pending.get("decision_id"),
                        user_id=user_id, mode=self.execution_mode, symbol=pending.get("symbol"),
                        reason="exchange_order_cancelled_after_accept",
                        order_id=pending.get("order_id"),
                    )
                    await self._clear_pending(user_id)
                elif order_status == "finish" and age >= 15.0:
                    # No proven position/settlement: keep the reservation, do not open a duplicate.
                    self.last_rejection = 'execution_requires_reconciliation'
            if self.pending_execution:
                self.audit.event(
                    "ENTRY_SKIPPED", pending.get("decision_id"), level="DEBUG", persist=False,
                    user_id=user_id, mode=self.execution_mode, symbol=snapshot.symbol,
                    reason="execution_pending", pending_symbol=pending.get("symbol"),
                    order_id=pending.get("order_id"),
                )
                return None

        if self._has_open_position(snapshot.symbol, user_id=user_id):
            self.audit.event(
                "ENTRY_SKIPPED", user_id, level="DEBUG", persist=False,
                user_id=user_id, mode=self.execution_mode, symbol=snapshot.symbol,
                reason="open_position_exists",
            )
            return None
        armed_monitor = bool(getattr(snapshot, 'armed_monitor', False))
        watch_monitor = bool(getattr(snapshot, 'watch_monitor', False))
        priority_tracking = armed_monitor or watch_monitor
        if not allow_entries or (getattr(snapshot, 'monitor_only', False) and not priority_tracking):
            self.audit.event(
                "ENTRY_SKIPPED", user_id, level="DEBUG", persist=False,
                user_id=user_id, mode=self.execution_mode, symbol=snapshot.symbol,
                reason="entries_disabled",
            )
            return None

        key = (snapshot.symbol, snapshot.timeframe)
        now = time.time()
        global_remaining = self.post_loss_global_cooldown_seconds - (now - self.last_loss_at) if self.last_loss_at else 0.0
        symbol_loss_at = self.last_symbol_loss_at.get(snapshot.symbol, 0.0)
        symbol_remaining = self.post_loss_symbol_cooldown_seconds - (now - symbol_loss_at) if symbol_loss_at else 0.0
        if global_remaining > 0:
            self.last_rejection = 'post_loss_global_cooldown'
            self.audit.event(
                'ENTRY_SKIPPED', user_id, level='DEBUG', persist=False,
                user_id=user_id, mode=self.execution_mode, symbol=snapshot.symbol,
                reason='post_loss_global_cooldown', remaining_seconds=round(global_remaining, 2),
            )
            return None
        if symbol_remaining > 0:
            self.last_rejection = 'post_loss_symbol_cooldown'
            self.audit.event(
                'ENTRY_SKIPPED', user_id, level='DEBUG', persist=False,
                user_id=user_id, mode=self.execution_mode, symbol=snapshot.symbol,
                reason='post_loss_symbol_cooldown', remaining_seconds=round(symbol_remaining, 2),
            )
            return None
        if (not priority_tracking) and now - self.last_decision.get(key, 0) < self.cooldown_seconds:
            self.audit.event(
                "ENTRY_SKIPPED", user_id, level="DEBUG", persist=False,
                user_id=user_id, mode=self.execution_mode, symbol=snapshot.symbol,
                reason="decision_cooldown",
                remaining_seconds=round(
                    self.cooldown_seconds - (now - self.last_decision.get(key, 0)), 2
                ),
            )
            return None
        if key in self.inflight:
            self.audit.event(
                "ENTRY_SKIPPED", user_id, level="DEBUG", persist=False,
                user_id=user_id, mode=self.execution_mode, symbol=snapshot.symbol,
                reason="decision_inflight",
            )
            return None

        self.inflight.add(key)
        decision_id = uuid4().hex
        accepted_signal = False
        terminal_event_emitted = False
        lifecycle_ctx = {}

        self.audit.event(
            "DECISION_START", decision_id, level="DEBUG", persist=False,
            user_id=user_id, mode=self.execution_mode, symbol=snapshot.symbol,
            timeframe=snapshot.timeframe, equity=equity,
            data_complete=getattr(snapshot, "data_complete", None),
            orderbook_valid=getattr(snapshot, "orderbook_valid", None),
        )

        try:
            # Snapshot acquisition precedes the shared runtime lock, account sync and
            # other users. Refresh only tracked entries here, BEFORE consuming a setup.
            if snapshot.symbol in self.armed_setups and self.execution_quote_provider is not None:
                try:
                    snapshot = await self._refresh_entry_quote(snapshot, decision_id, user_id)
                except Exception as exc:
                    self.last_rejection = "execution_quote_refresh_pending"
                    self.audit.event(
                        "EXECUTION_QUOTE_PENDING", decision_id, user_id=user_id,
                        mode=self.execution_mode, symbol=snapshot.symbol,
                        reason=self.last_rejection, error=type(exc).__name__,
                    )
                    return None  # Keep ARMED for the next monitor tick, until its normal expiry.
            # ------------------------- SIGNAL STAGE -------------------------
            # Discovery and trigger-followup deliberately run on different paths.
            # Once a setup is ARMED, the priority monitor supplies only live depth
            # plus CLOSED 1m candles; there is no reason to wait for another full
            # scanner rotation or to recompute the 5m/15m/1h regime.
            priority_armed = bool(getattr(snapshot, "armed_monitor", False))
            priority_watch = bool(getattr(snapshot, "watch_monitor", False))
            intent = None
            strategy_trace = {}
            regime = None
            regime_meta = {}
            router_kwargs = {"snapshot": snapshot}

            if priority_watch:
                watch = self.watching_setups.get(snapshot.symbol)
                if watch is None:
                    return None
                lifecycle_ctx = self._lifecycle_context(watch=watch, decision_id=decision_id)
                status, armed, watch_trace = self.router.advance_watch(watch, snapshot)
                strategy_trace = getattr(self.router, "last_trace", {}) or {}
                if status == "cancelled":
                    self.watching_setups.pop(snapshot.symbol, None)
                    reason = str((watch_trace or {}).get("reason") or "watch_cancelled")
                    if hasattr(self.router, "consume_watch"):
                        self.router.consume_watch(watch, reason)
                    self._schedule_watch_terminal(user_id, watch, reason)
                    self._funnel("watch_cancelled", reason, user_id=user_id, symbol=snapshot.symbol)
                    self.last_rejection = reason
                    self.audit.event(
                        "SETUP_WATCH_CANCELLED", decision_id, user_id=user_id,
                        mode=self.execution_mode, symbol=snapshot.symbol,
                        lifecycle_id=lifecycle_ctx.get("lifecycle_id"),
                        watch_id=getattr(watch, "watch_id", None),
                        stage="CANCELLED", stage_order=LIFECYCLE_STAGE_ORDER["CANCELLED"],
                        strategy=watch.strategy.value, direction=watch.direction.value, reason=reason,
                        source="watch_priority_monitor", trace=watch_trace,
                    )
                    self._terminal_lifecycle(
                        user_id, lifecycle_ctx=lifecycle_ctx, symbol=snapshot.symbol,
                        decision_id=decision_id, reason=reason, strategy=watch.strategy,
                        direction=watch.direction, stage="CANCELLED", details={"source": "watch_priority_monitor"},
                    )
                    return None
                if status == "pending":
                    reason = str((watch_trace or {}).get("reason") or "watch_pending")
                    self.last_rejection = reason
                    self.audit.event(
                        "SETUP_WATCH_PENDING", decision_id, level="DEBUG", persist=False,
                        user_id=user_id, mode=self.execution_mode, symbol=snapshot.symbol,
                        lifecycle_id=lifecycle_ctx.get("lifecycle_id"),
                        watch_id=getattr(watch, "watch_id", None), reason=reason, trace=watch_trace,
                    )
                    self._emit_lifecycle_progress(
                        "SETUP_WATCH_PROGRESS", phase="WATCHING", decision_id=decision_id,
                        user_id=user_id, symbol=snapshot.symbol, lifecycle_ctx=lifecycle_ctx,
                        strategy=watch.strategy, direction=watch.direction, reason=reason, trace=watch_trace,
                    )
                    return None
                if armed is None:
                    return None
                self.watching_setups.pop(snapshot.symbol, None)
                if hasattr(self.router, "consume_watch"):
                    self.router.consume_watch(watch, "setup_armed")
                self._schedule_watch_terminal(user_id, watch, "setup_armed")
                armed = self._ensure_armed_lifecycle(armed, watch=watch)
                lifecycle_ctx = self._lifecycle_context(watch=watch, setup=armed, decision_id=decision_id)
                self.armed_setups[snapshot.symbol] = armed
                await self._persist_armed_active(user_id, armed)
                self.last_rejection = "setup_armed_waiting_trigger"
                self._funnel("armed", user_id=user_id, symbol=snapshot.symbol)
                self.audit.event(
                    "SETUP_ARMED", decision_id, user_id=user_id, mode=self.execution_mode,
                    symbol=snapshot.symbol, lifecycle_id=lifecycle_ctx.get("lifecycle_id"),
                    watch_id=lifecycle_ctx.get("watch_id"), setup_id=armed.setup_id,
                    stage="ARMED", stage_order=LIFECYCLE_STAGE_ORDER["ARMED"],
                    previous_stage="WATCHING", strategy=armed.strategy.value,
                    direction=armed.direction.value, quality=armed.quality,
                    trigger_price=armed.trigger_price, invalidation_price=armed.invalidation_price,
                    entry_zone_low=armed.entry_zone_low, entry_zone_high=armed.entry_zone_high,
                    stop_price=armed.stop_price, target_price=armed.target_price,
                    expires_at_ms=armed.expires_at_ms, priority_monitor=True,
                    source="watch_priority_monitor", watch_trace=watch_trace,
                )
                self._schedule_lifecycle_stage(
                    user_id, lifecycle_id=lifecycle_ctx.get("lifecycle_id"), stage="ARMED",
                    symbol=snapshot.symbol, strategy=armed.strategy, direction=armed.direction,
                    decision_id=decision_id, watch_id=lifecycle_ctx.get("watch_id"),
                    setup_id=armed.setup_id, reason=str((watch_trace or {}).get("reason") or "setup_armed"),
                    details={"source": "watch_priority_monitor", "quality": armed.quality},
                )
                return None

            if priority_armed:
                armed = self.armed_setups.get(snapshot.symbol)
                if armed is None:
                    return None
                armed = self._ensure_armed_lifecycle(armed)
                self.armed_setups[snapshot.symbol] = armed
                lifecycle_ctx = self._lifecycle_context(setup=armed, decision_id=decision_id)
                status, intent, armed_trace = self.router.trigger_armed(armed, snapshot, decision_id)
                strategy_trace = getattr(self.router, "last_trace", {}) or {}
                if status == "cancelled":
                    self.armed_setups.pop(snapshot.symbol, None)
                    reason = str((armed_trace or {}).get("reason") or "armed_setup_cancelled")
                    self._schedule_armed_terminal(user_id, armed, reason)
                    self.last_rejection = reason
                    self.last_decision[key] = now
                    self._funnel("armed_cancelled", reason, user_id=user_id, symbol=snapshot.symbol)
                    self.audit.event(
                        "SETUP_CANCELLED", decision_id, user_id=user_id,
                        mode=self.execution_mode, symbol=snapshot.symbol,
                        lifecycle_id=lifecycle_ctx.get("lifecycle_id"),
                        watch_id=lifecycle_ctx.get("watch_id"), setup_id=getattr(armed, "setup_id", None),
                        stage="CANCELLED", stage_order=LIFECYCLE_STAGE_ORDER["CANCELLED"],
                        strategy=armed.strategy.value, direction=armed.direction.value, reason=reason,
                        source="armed_priority_monitor", trace=armed_trace,
                    )
                    self._terminal_lifecycle(
                        user_id, lifecycle_ctx=lifecycle_ctx, symbol=snapshot.symbol,
                        decision_id=decision_id, reason=reason, strategy=armed.strategy,
                        direction=armed.direction, stage="CANCELLED", details={"source": "armed_priority_monitor"},
                    )
                    return None
                if status == "pending":
                    reason = str((armed_trace or {}).get("reason") or "armed_waiting")
                    self.last_rejection = reason
                    # High-frequency priority polls must not inflate the rejection
                    # funnel. The setup is counted once when armed and once when it
                    # reaches a terminal state.
                    self.audit.event(
                        "SETUP_PRIORITY_PENDING", decision_id, level="DEBUG", persist=False,
                        user_id=user_id, mode=self.execution_mode, symbol=snapshot.symbol,
                        lifecycle_id=lifecycle_ctx.get("lifecycle_id"),
                        watch_id=lifecycle_ctx.get("watch_id"),
                        setup_id=getattr(armed, "setup_id", None), reason=reason, trace=armed_trace,
                    )
                    self._emit_lifecycle_progress(
                        "SETUP_ARMED_PROGRESS", phase="ARMED", decision_id=decision_id,
                        user_id=user_id, symbol=snapshot.symbol, lifecycle_ctx=lifecycle_ctx,
                        strategy=armed.strategy, direction=armed.direction, reason=reason, trace=armed_trace,
                    )
                    return None
                self.armed_setups.pop(snapshot.symbol, None)
                self._schedule_armed_terminal(user_id, armed, "setup_triggered")
                self._funnel("triggered", user_id=user_id, symbol=snapshot.symbol)
                lifecycle_ctx = self._lifecycle_context(setup=armed, intent=intent, decision_id=decision_id)
                self.audit.event(
                    "SETUP_TRIGGERED", decision_id, user_id=user_id,
                    mode=self.execution_mode, symbol=snapshot.symbol,
                    lifecycle_id=lifecycle_ctx.get("lifecycle_id"), watch_id=lifecycle_ctx.get("watch_id"),
                    setup_id=getattr(armed, "setup_id", None), stage="TRIGGERED",
                    stage_order=LIFECYCLE_STAGE_ORDER["TRIGGERED"],
                    strategy=getattr(getattr(intent, "strategy", None), "value", None),
                    direction=getattr(getattr(intent, "direction", None), "value", None),
                    source="armed_priority_monitor", trace=armed_trace,
                )
                self._schedule_lifecycle_stage(
                    user_id, lifecycle_id=lifecycle_ctx.get("lifecycle_id"), stage="TRIGGERED",
                    symbol=snapshot.symbol, strategy=getattr(intent, "strategy", None),
                    direction=getattr(intent, "direction", None), decision_id=decision_id,
                    watch_id=lifecycle_ctx.get("watch_id"), setup_id=lifecycle_ctx.get("setup_id"),
                    reason=str((armed_trace or {}).get("reason") or "setup_triggered"),
                    details={"source": "armed_priority_monitor"},
                )
            else:
                regime = self.regime_engine.evaluate_snapshot(snapshot)
                regime_meta = getattr(self.regime_engine, "last_metadata", {}) or {}
                rf = regime_meta.get("features") or {}
                rs = regime_meta.get("state") or {}
                feature_summary = {
                    k: rf.get(k) for k in (
                        "adx", "choppiness", "efficiency_ratio", "atr_pct",
                        "wick_instability", "body_quality", "breakout_failure_ratio",
                        "ema_stack_alignment", "ema_bullish_alignment", "ema_bearish_alignment",
                        "ema_alignment_edge", "trend_bias", "btc_shock_ratio",
                    ) if k in rf
                }
                state_summary = {
                    k: rs.get(k) for k in (
                        "active", "candidate", "pending", "pending_count",
                        "bars", "cooldown", "changed",
                    ) if k in rs
                }
                self.audit.event(
                    "REGIME_EVALUATED", decision_id, user_id=user_id,
                    mode=self.execution_mode, symbol=snapshot.symbol,
                    state=regime.global_state.value, score=regime.core_score,
                    candidate=regime_meta.get("candidate"), active=regime_meta.get("active"),
                    confidence=regime_meta.get("confidence"), scores=regime_meta.get("scores"),
                    regime_direction=getattr(getattr(regime, "direction", None), "value", str(getattr(regime, "direction", "UNKNOWN"))),
                    features=feature_summary, state_machine=state_summary,
                    breakout_allowed=regime.breakout_allowed, sweep_allowed=regime.sweep_allowed,
                    hard_block=regime.hard_block, risk_multiplier=regime.risk_multiplier,
                )

                self._funnel("analyzed", user_id=user_id, symbol=snapshot.symbol)
                try:
                    router_params = inspect.signature(self.router.evaluate).parameters.values()
                    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in router_params) or any(
                        p.name == "regime_metadata" for p in router_params
                    ):
                        router_kwargs["regime_metadata"] = regime_meta
                except (TypeError, ValueError):
                    pass

                supports_armed = (
                    self.armed_entry_enabled
                    and hasattr(self.router, "discover_armed")
                    and hasattr(self.router, "trigger_armed")
                    and bool(getattr(snapshot, "timeframes", None))
                )
                if supports_armed:
                    armed = self.armed_setups.get(snapshot.symbol)
                    if armed is not None:
                        armed = self._ensure_armed_lifecycle(armed)
                        self.armed_setups[snapshot.symbol] = armed
                        lifecycle_ctx = self._lifecycle_context(setup=armed, decision_id=decision_id)
                        status, intent, armed_trace = self.router.trigger_armed(armed, snapshot, decision_id)
                        strategy_trace = getattr(self.router, "last_trace", {}) or {}
                        if status == "cancelled":
                            self.armed_setups.pop(snapshot.symbol, None)
                            reason = str((armed_trace or {}).get("reason") or "armed_setup_cancelled")
                            self._schedule_armed_terminal(user_id, armed, reason)
                            self.last_rejection = reason
                            self.last_decision[key] = now
                            self._funnel("armed_cancelled", reason, user_id=user_id, symbol=snapshot.symbol)
                            self.audit.event(
                                "SETUP_CANCELLED", decision_id, user_id=user_id,
                                mode=self.execution_mode, symbol=snapshot.symbol,
                                lifecycle_id=lifecycle_ctx.get("lifecycle_id"),
                                watch_id=lifecycle_ctx.get("watch_id"), setup_id=getattr(armed, "setup_id", None),
                                stage="CANCELLED", stage_order=LIFECYCLE_STAGE_ORDER["CANCELLED"],
                                strategy=armed.strategy.value, direction=armed.direction.value, reason=reason,
                                source="scanner", trace=armed_trace,
                            )
                            self._terminal_lifecycle(
                                user_id, lifecycle_ctx=lifecycle_ctx, symbol=snapshot.symbol,
                                decision_id=decision_id, reason=reason, strategy=armed.strategy,
                                direction=armed.direction, stage="CANCELLED", details={"source": "scanner"},
                            )
                            return None
                        if status == "pending":
                            reason = str((armed_trace or {}).get("reason") or "armed_waiting")
                            self.last_rejection = reason
                            self._funnel("armed_pending", reason, user_id=user_id, symbol=snapshot.symbol)
                            self.audit.event(
                                "SETUP_PENDING", decision_id, level="DEBUG", persist=False,
                                user_id=user_id, mode=self.execution_mode, symbol=snapshot.symbol,
                                lifecycle_id=lifecycle_ctx.get("lifecycle_id"), watch_id=lifecycle_ctx.get("watch_id"),
                                setup_id=getattr(armed, "setup_id", None), reason=reason, trace=armed_trace,
                            )
                            self._emit_lifecycle_progress(
                                "SETUP_ARMED_PROGRESS", phase="ARMED", decision_id=decision_id,
                                user_id=user_id, symbol=snapshot.symbol, lifecycle_ctx=lifecycle_ctx,
                                strategy=armed.strategy, direction=armed.direction, reason=reason, trace=armed_trace,
                            )
                            return None
                        self.armed_setups.pop(snapshot.symbol, None)
                        self._schedule_armed_terminal(user_id, armed, "setup_triggered")
                        self._funnel("triggered", user_id=user_id, symbol=snapshot.symbol)
                        lifecycle_ctx = self._lifecycle_context(setup=armed, intent=intent, decision_id=decision_id)
                        self.audit.event(
                            "SETUP_TRIGGERED", decision_id, user_id=user_id,
                            mode=self.execution_mode, symbol=snapshot.symbol,
                            lifecycle_id=lifecycle_ctx.get("lifecycle_id"), watch_id=lifecycle_ctx.get("watch_id"),
                            setup_id=getattr(armed, "setup_id", None), stage="TRIGGERED",
                            stage_order=LIFECYCLE_STAGE_ORDER["TRIGGERED"],
                            strategy=getattr(getattr(intent, "strategy", None), "value", None),
                            direction=getattr(getattr(intent, "direction", None), "value", None),
                            source="scanner", trace=armed_trace,
                        )
                        self._schedule_lifecycle_stage(
                            user_id, lifecycle_id=lifecycle_ctx.get("lifecycle_id"), stage="TRIGGERED",
                            symbol=snapshot.symbol, strategy=getattr(intent, "strategy", None),
                            direction=getattr(intent, "direction", None), decision_id=decision_id,
                            watch_id=lifecycle_ctx.get("watch_id"), setup_id=lifecycle_ctx.get("setup_id"),
                            reason=str((armed_trace or {}).get("reason") or "setup_triggered"),
                            details={"source": "scanner"},
                        )
                    else:
                        existing_watch = self.watching_setups.get(snapshot.symbol)
                        if existing_watch is not None:
                            self.audit.event(
                                "SETUP_WATCH_PRESENT", decision_id, level="DEBUG", persist=False,
                                user_id=user_id, mode=self.execution_mode, symbol=snapshot.symbol,
                                watch_id=getattr(existing_watch, "watch_id", None),
                                strategy=getattr(getattr(existing_watch, "strategy", None), "value", None),
                                direction=getattr(getattr(existing_watch, "direction", None), "value", None),
                            )
                            return None
                        armed = self.router.discover_armed(
                            regime, snapshot, snapshot.symbol, snapshot.timeframe,
                            regime_metadata=regime_meta,
                        )
                        strategy_trace = getattr(self.router, "last_trace", {}) or {}
                        if armed is not None:
                            armed = self._ensure_armed_lifecycle(armed)
                            lifecycle_ctx = self._lifecycle_context(setup=armed, decision_id=decision_id)
                            self.armed_setups[snapshot.symbol] = armed
                            await self._persist_armed_active(user_id, armed)
                            self.last_rejection = "setup_armed_waiting_trigger"
                            self._funnel("armed", user_id=user_id, symbol=snapshot.symbol)
                            self.audit.event(
                                "SETUP_ARMED", decision_id, user_id=user_id,
                                mode=self.execution_mode, symbol=snapshot.symbol,
                                lifecycle_id=lifecycle_ctx.get("lifecycle_id"), watch_id=lifecycle_ctx.get("watch_id"),
                                setup_id=armed.setup_id, stage="ARMED", stage_order=LIFECYCLE_STAGE_ORDER["ARMED"],
                                previous_stage="DISCOVERY", strategy=armed.strategy.value,
                                direction=armed.direction.value, quality=armed.quality,
                                trigger_price=armed.trigger_price, invalidation_price=armed.invalidation_price,
                                entry_zone_low=armed.entry_zone_low, entry_zone_high=armed.entry_zone_high,
                                stop_price=armed.stop_price, target_price=armed.target_price,
                                expires_at_ms=armed.expires_at_ms, priority_monitor=True, source="scanner_complete_setup",
                            )
                            self._schedule_lifecycle_stage(
                                user_id, lifecycle_id=lifecycle_ctx.get("lifecycle_id"), stage="ARMED",
                                symbol=snapshot.symbol, strategy=armed.strategy, direction=armed.direction,
                                decision_id=decision_id, watch_id=lifecycle_ctx.get("watch_id"),
                                setup_id=armed.setup_id, reason="scanner_complete_setup",
                                details={"source": "scanner_complete_setup", "quality": armed.quality},
                            )
                            return None
                        watch = None
                        if hasattr(self.router, "discover_watch"):
                            watch = self.router.discover_watch(
                                regime, snapshot, snapshot.symbol, snapshot.timeframe,
                                regime_metadata=regime_meta,
                            )
                            strategy_trace = getattr(self.router, "last_trace", {}) or strategy_trace
                        if watch is not None:
                            lifecycle_ctx = self._lifecycle_context(watch=watch, decision_id=decision_id)
                            self.watching_setups[snapshot.symbol] = watch
                            await self._persist_watch_active(user_id, watch)
                            self.last_rejection = "setup_watching"
                            self._funnel("watching", user_id=user_id, symbol=snapshot.symbol)
                            watch_trace_payload = strategy_trace.get("watch") if isinstance(strategy_trace, dict) else {}
                            self.audit.event(
                                "SETUP_WATCHING", decision_id, user_id=user_id, mode=self.execution_mode,
                                symbol=snapshot.symbol, lifecycle_id=lifecycle_ctx.get("lifecycle_id"),
                                watch_id=watch.watch_id, stage="WATCHING", stage_order=LIFECYCLE_STAGE_ORDER["WATCHING"],
                                strategy=watch.strategy.value, direction=watch.direction.value,
                                created_at_ms=watch.created_at_ms, expires_at_ms=watch.expires_at_ms,
                                reasons=list(watch.reasons), trace=watch_trace_payload,
                            )
                            self._schedule_lifecycle_stage(
                                user_id, lifecycle_id=lifecycle_ctx.get("lifecycle_id"), stage="WATCHING",
                                symbol=snapshot.symbol, strategy=watch.strategy, direction=watch.direction,
                                decision_id=decision_id, watch_id=watch.watch_id, reason="setup_watching",
                                details={"reasons": list(watch.reasons)},
                            )
                            return None
                        if not self.legacy_entry_fallback_enabled:
                            watch_trace = strategy_trace.get("watch") if isinstance(strategy_trace, dict) else {}
                            armed_trace = strategy_trace.get("armed") if isinstance(strategy_trace, dict) else {}
                            rejection_reason = str((watch_trace or {}).get("reason") or (armed_trace or {}).get("reason") or "no_watchable_precursor")
                            self.last_rejection = rejection_reason
                            self.last_decision[key] = now
                            self._funnel("strategy_rejected", rejection_reason, user_id=user_id, symbol=snapshot.symbol)
                            self.audit.event(
                                "STRATEGY_EVALUATED", decision_id, user_id=user_id,
                                mode=self.execution_mode, symbol=snapshot.symbol,
                                regime=regime_meta.get("active"),
                                selected_direction=None, trace=strategy_trace,
                            )
                            self.audit.event(
                                "SIGNAL_REJECTED", decision_id, user_id=user_id,
                                mode=self.execution_mode, symbol=snapshot.symbol,
                                regime=regime_meta.get("active"), reason=rejection_reason,
                            )
                            return None

                # Legacy immediate-entry path remains available for tests/custom routers
                # and can be explicitly enabled in production as a rollback switch.
                if intent is None:
                    intent = self.router.evaluate(
                        regime, snapshot.candles, decision_id, snapshot.symbol,
                        snapshot.timeframe, snapshot.last, **router_kwargs,
                    )
                    strategy_trace = getattr(self.router, "last_trace", {}) or {}

            compact_trace = {
                "selected": strategy_trace.get("selected"),
                "reason": strategy_trace.get("reason"),
            }
            for name in ("breakout", "sweep", "armed"):
                branch = strategy_trace.get(name)
                if isinstance(branch, dict):
                    compact_trace[name] = {
                        k: branch.get(k)
                        for k in ("accepted", "reason", "score", "quality", "side", "direction", "status")
                        if k in branch
                    }
            self.audit.event(
                "STRATEGY_EVALUATED", decision_id, user_id=user_id,
                mode=self.execution_mode, symbol=snapshot.symbol,
                regime=regime_meta.get("active"),
                regime_direction=getattr(getattr(regime, "direction", None), "value", str(getattr(regime, "direction", "UNKNOWN"))),
                selected_direction=(
                    getattr(getattr(intent, "direction", None), "value", None)
                    if intent is not None else None
                ),
                trace=compact_trace,
            )

            if not intent:
                self.last_decision[key] = now
                rejection_reason = "no_valid_setup"
                for name in ("breakout", "sweep", "armed"):
                    branch = strategy_trace.get(name)
                    if isinstance(branch, dict) and branch.get("reason") not in (
                        None, "not_run", "regime_breakout_not_allowed",
                        "regime_sweep_not_allowed",
                    ):
                        rejection_reason = str(branch.get("reason"))
                        break
                self.last_rejection = rejection_reason
                self._funnel("strategy_rejected", rejection_reason, user_id=user_id, symbol=snapshot.symbol)
                self.audit.event(
                    "SIGNAL_REJECTED", decision_id, user_id=user_id,
                    mode=self.execution_mode, symbol=snapshot.symbol,
                    regime=regime_meta.get("active"), reason=rejection_reason,
                )
                return None

            lifecycle_ctx = self._lifecycle_context(intent=intent, decision_id=decision_id)
            # A worker restart or delayed persistence must not bypass the
            # one-position-per-user rule before placing a second order.
            if user_id and (await asyncio.to_thread(
                    self.db.find_one, 'positions', {'user_id': user_id, 'status': 'OPEN'})):
                self.last_rejection = 'open_position_exists'
                terminal_event_emitted = True
                self._funnel("execution_rejected", "open_position_exists", user_id=user_id, symbol=snapshot.symbol)
                self.audit.event(
                    'EXECUTION_REJECTED', decision_id, user_id=user_id, mode=self.execution_mode,
                    symbol=snapshot.symbol, lifecycle_id=lifecycle_ctx.get("lifecycle_id"),
                    watch_id=lifecycle_ctx.get("watch_id"), setup_id=lifecycle_ctx.get("setup_id"),
                    stage="REJECTED", strategy=getattr(intent.strategy, "value", str(intent.strategy)),
                    direction=getattr(intent.direction, "value", str(intent.direction)), reason='open_position_exists',
                )
                self._terminal_lifecycle(
                    user_id, lifecycle_ctx=lifecycle_ctx, symbol=snapshot.symbol, decision_id=decision_id,
                    reason='open_position_exists', strategy=intent.strategy, direction=intent.direction,
                )
                return {'accepted': False, 'filled': False, 'reason': 'open_position_exists'}
            if user_id and (await asyncio.to_thread(
                    self.db.find_one, 'execution_pending', {'user_id': user_id, 'active': True})):
                self.last_rejection = 'execution_pending'
                terminal_event_emitted = True
                self._funnel("execution_rejected", "execution_pending", user_id=user_id, symbol=snapshot.symbol)
                self.audit.event(
                    'EXECUTION_REJECTED', decision_id, user_id=user_id, mode=self.execution_mode,
                    symbol=snapshot.symbol, lifecycle_id=lifecycle_ctx.get("lifecycle_id"),
                    watch_id=lifecycle_ctx.get("watch_id"), setup_id=lifecycle_ctx.get("setup_id"),
                    stage="REJECTED", strategy=getattr(intent.strategy, "value", str(intent.strategy)),
                    direction=getattr(intent.direction, "value", str(intent.direction)), reason='execution_pending',
                )
                self._terminal_lifecycle(
                    user_id, lifecycle_ctx=lifecycle_ctx, symbol=snapshot.symbol, decision_id=decision_id,
                    reason='execution_pending', strategy=intent.strategy, direction=intent.direction,
                )
                return {'accepted': False, 'filled': False, 'reason': 'execution_pending'}
            # These durable checks may be slow; fetch depth again afterwards and
            # run ALL existing price, RR, slippage, noise and book checks on that quote.
            if self.execution_quote_provider is not None:
                try:
                    snapshot = await self._refresh_entry_quote(snapshot, decision_id, user_id)
                except Exception as exc:
                    self.last_rejection = "execution_quote_refresh_failed"
                    terminal_event_emitted = True
                    self._funnel("execution_rejected", self.last_rejection, user_id=user_id, symbol=snapshot.symbol)
                    self.audit.event(
                        "EXECUTION_REJECTED", decision_id, user_id=user_id, mode=self.execution_mode,
                        symbol=snapshot.symbol, reason=self.last_rejection, error=type(exc).__name__,
                        lifecycle_id=lifecycle_ctx.get("lifecycle_id"), stage="REJECTED",
                    )
                    self._terminal_lifecycle(
                        user_id, lifecycle_ctx=lifecycle_ctx, symbol=snapshot.symbol, decision_id=decision_id,
                        reason=self.last_rejection, strategy=intent.strategy, direction=intent.direction,
                    )
                    return {"accepted": False, "filled": False, "reason": self.last_rejection}
            signal_entry = float(intent.entry_price)
            try:
                executable = float(snapshot.ask if intent.direction == Direction.LONG else snapshot.bid)
                if math.isfinite(executable) and executable > 0:
                    # Size against the expected executable price, including DEMO's explicit slippage.
                    slip = float(getattr(self.execution, 'slippage_bps', 0)) / 10000
                    executable *= 1 + slip if intent.direction == Direction.LONG else 1 - slip
                    intent = replace(intent, entry_price=executable)
            except (ValueError, TypeError):
                pass  # The market guard below emits the terminal rejection.
            entry = float(intent.entry_price)
            stop = float(intent.stop_price)
            target = float(intent.target_price)
            risk_distance = abs(entry - stop)
            reward_distance = abs(target - entry)
            execution_rr = reward_distance / max(risk_distance, 1e-12)
            geometry_ok = (
                math.isfinite(execution_rr)
                and execution_rr > 0
                and (
                    (intent.direction == Direction.LONG and stop < entry < target)
                    or (intent.direction == Direction.SHORT and target < entry < stop)
                )
            )
            if not geometry_ok:
                self.last_decision[key] = now
                self.last_rejection = 'invalid_trade_geometry' 
                self.audit.event(
                    "SIGNAL_REJECTED", decision_id, user_id=user_id,
                    mode=self.execution_mode, symbol=snapshot.symbol,
                    lifecycle_id=lifecycle_ctx.get("lifecycle_id"), watch_id=lifecycle_ctx.get("watch_id"),
                    setup_id=lifecycle_ctx.get("setup_id"), stage="REJECTED",
                    strategy=getattr(intent.strategy, "value", str(intent.strategy)),
                    direction=getattr(intent.direction, "value", str(intent.direction)),
                    reason="invalid_trade_geometry", entry_price=entry,
                    stop_price=stop, target_price=target, execution_rr=execution_rr,
                )
                self._terminal_lifecycle(
                    user_id, lifecycle_ctx=lifecycle_ctx, symbol=snapshot.symbol, decision_id=decision_id,
                    reason="invalid_trade_geometry", strategy=intent.strategy, direction=intent.direction,
                    details={"execution_rr": execution_rr},
                )
                return None

            # Entry-quality guard. The strategy is evaluated on a closed 5m candle,
            # while execution uses a newer bid/ask. If price has already chased the
            # signal or invalidated the confirmation, wait for a fresh setup instead
            # of buying/selling the exhausted move.
            metadata = getattr(intent, 'metadata', {}) or {}
            armed_breakout = (
                intent.strategy == Strategy.BREAKOUT_RETEST
                and metadata.get("entry_model") == "armed_v6"
            )
            try:
                atr_value = float(metadata.get('atr_value') or 0.0)
            except (TypeError, ValueError):
                atr_value = 0.0
            if (not math.isfinite(atr_value) or atr_value <= 0) and signal_entry > 0:
                try:
                    atr_value = float(metadata.get('atr_pct') or 0.0) * signal_entry
                except (TypeError, ValueError):
                    atr_value = 0.0
            if math.isfinite(atr_value) and atr_value > 0:
                directional_move = ((entry - signal_entry) if intent.direction == Direction.LONG
                                    else (signal_entry - entry))
                adverse_reversal = ((signal_entry - entry) if intent.direction == Direction.LONG
                                    else (entry - signal_entry))
                chase_atr = max(0.0, directional_move) / atr_value
                adverse_atr = max(0.0, adverse_reversal) / atr_value
                if chase_atr > self.entry_max_chase_atr:
                    self.last_decision[key] = now
                    self.last_rejection = 'entry_chased_after_signal'
                    self.audit.event(
                        'SIGNAL_REJECTED', decision_id, user_id=user_id,
                        mode=self.execution_mode, symbol=snapshot.symbol,
                        lifecycle_id=lifecycle_ctx.get("lifecycle_id"), watch_id=lifecycle_ctx.get("watch_id"),
                        setup_id=lifecycle_ctx.get("setup_id"), stage="REJECTED",
                        strategy=getattr(intent.strategy, 'value', str(intent.strategy)),
                        direction=getattr(intent.direction, 'value', str(intent.direction)),
                        reason='entry_chased_after_signal', entry_price=entry,
                        signal_entry_price=signal_entry, atr_value=atr_value,
                        move_atr=round(chase_atr, 4), maximum_atr=self.entry_max_chase_atr,
                    )
                    self._terminal_lifecycle(
                        user_id, lifecycle_ctx=lifecycle_ctx, symbol=snapshot.symbol, decision_id=decision_id,
                        reason='entry_chased_after_signal', strategy=intent.strategy, direction=intent.direction,
                        details={"move_atr": round(chase_atr, 4)},
                    )
                    return None
                if adverse_atr > self.entry_max_adverse_reversal_atr:
                    self.last_decision[key] = now
                    self.last_rejection = 'entry_confirmation_lost_before_fill'
                    self.audit.event(
                        'SIGNAL_REJECTED', decision_id, user_id=user_id,
                        mode=self.execution_mode, symbol=snapshot.symbol,
                        lifecycle_id=lifecycle_ctx.get("lifecycle_id"), watch_id=lifecycle_ctx.get("watch_id"),
                        setup_id=lifecycle_ctx.get("setup_id"), stage="REJECTED",
                        strategy=getattr(intent.strategy, 'value', str(intent.strategy)),
                        direction=getattr(intent.direction, 'value', str(intent.direction)),
                        reason='entry_confirmation_lost_before_fill', entry_price=entry,
                        signal_entry_price=signal_entry, atr_value=atr_value,
                        move_atr=round(adverse_atr, 4), maximum_atr=self.entry_max_adverse_reversal_atr,
                    )
                    self._terminal_lifecycle(
                        user_id, lifecycle_ctx=lifecycle_ctx, symbol=snapshot.symbol, decision_id=decision_id,
                        reason='entry_confirmation_lost_before_fill', strategy=intent.strategy, direction=intent.direction,
                        details={"move_atr": round(adverse_atr, 4)},
                    )
                    return None

                spread_abs = 0.0
                try:
                    spread_abs = max(0.0, float(snapshot.ask) - float(snapshot.bid))
                except (TypeError, ValueError):
                    pass
                minimum_stop_atr = self.entry_min_stop_atr
                if armed_breakout:
                    micro = metadata.get("micro_confirmation") or {}
                    adaptive_confirmation = (
                        isinstance(micro, dict)
                        and micro.get("confirmation_mode") == "postarm_closed_1m_strong_shape"
                    )
                    breakout_floor = (
                        breakout.ARMED_ADAPTIVE_MIN_STOP_ATR_5M
                        if adaptive_confirmation
                        else breakout.ARMED_MIN_STOP_ATR_5M
                    )
                    minimum_stop_atr = max(minimum_stop_atr, breakout_floor)
                minimum_stop_distance = max(
                    atr_value * minimum_stop_atr,
                    spread_abs * self.entry_min_stop_spreads,
                )
                if minimum_stop_distance > 0 and risk_distance < minimum_stop_distance:
                    self.last_decision[key] = now
                    self.last_rejection = 'stop_inside_market_noise'
                    self.audit.event(
                        'SIGNAL_REJECTED', decision_id, user_id=user_id,
                        mode=self.execution_mode, symbol=snapshot.symbol,
                        lifecycle_id=lifecycle_ctx.get("lifecycle_id"), watch_id=lifecycle_ctx.get("watch_id"),
                        setup_id=lifecycle_ctx.get("setup_id"), stage="REJECTED",
                        strategy=getattr(intent.strategy, 'value', str(intent.strategy)),
                        direction=getattr(intent.direction, 'value', str(intent.direction)),
                        reason='stop_inside_market_noise', entry_price=entry, stop_price=stop,
                        stop_distance=risk_distance, minimum_stop_distance=minimum_stop_distance,
                        stop_atr=round(risk_distance / atr_value, 4),
                        minimum_stop_atr=minimum_stop_atr, spread_abs=spread_abs,
                    )
                    self._terminal_lifecycle(
                        user_id, lifecycle_ctx=lifecycle_ctx, symbol=snapshot.symbol, decision_id=decision_id,
                        reason='stop_inside_market_noise', strategy=intent.strategy, direction=intent.direction,
                        details={"stop_atr": round(risk_distance / atr_value, 4)},
                    )
                    return None

            # Reject only a severe top-20 order-book conflict. Mild imbalance is
            # noisy/spoofable and is logged by the market layer, but a large
            # opposite wall immediately before execution is a useful final veto.
            bids = list(getattr(snapshot, 'bids', []) or [])[:20]
            asks = list(getattr(snapshot, 'asks', []) or [])[:20]
            if len(bids) >= 5 and len(asks) >= 5 and self.entry_orderbook_conflict_threshold > 0:
                try:
                    bid_value = sum(float(level[0]) * float(level[1]) for level in bids)
                    ask_value = sum(float(level[0]) * float(level[1]) for level in asks)
                    total_value = bid_value + ask_value
                    imbalance = ((bid_value - ask_value) / total_value) if total_value > 0 else 0.0
                except (TypeError, ValueError, IndexError):
                    imbalance = 0.0
                conflicts = (
                    (intent.direction == Direction.LONG and imbalance <= -self.entry_orderbook_conflict_threshold)
                    or (intent.direction == Direction.SHORT and imbalance >= self.entry_orderbook_conflict_threshold)
                )
                if conflicts:
                    self.last_decision[key] = now
                    self.last_rejection = 'orderbook_conflict'
                    self.audit.event(
                        'SIGNAL_REJECTED', decision_id, user_id=user_id,
                        mode=self.execution_mode, symbol=snapshot.symbol,
                        lifecycle_id=lifecycle_ctx.get("lifecycle_id"), watch_id=lifecycle_ctx.get("watch_id"),
                        setup_id=lifecycle_ctx.get("setup_id"), stage="REJECTED",
                        strategy=getattr(intent.strategy, 'value', str(intent.strategy)),
                        reason='orderbook_conflict', direction=getattr(intent.direction, 'value', str(intent.direction)),
                        orderbook_imbalance=round(imbalance, 4),
                        threshold=self.entry_orderbook_conflict_threshold, levels=20,
                    )
                    self._terminal_lifecycle(
                        user_id, lifecycle_ctx=lifecycle_ctx, symbol=snapshot.symbol, decision_id=decision_id,
                        reason='orderbook_conflict', strategy=intent.strategy, direction=intent.direction,
                        details={"orderbook_imbalance": round(imbalance, 4)},
                    )
                    return None

            # Signal SL/TP are anchored to the closed candle. A delayed quote can
            # improve apparent RR by bringing entry right next to the old stop.
            # Keep the original structure and wait for a new setup instead of
            # widening a protective stop after the signal was generated.
            planned_stop_pct = (getattr(intent, 'metadata', {}) or {}).get('sl_pct')
            if planned_stop_pct is not None:
                try:
                    minimum_stop_pct = 0.80 * float(planned_stop_pct)
                except (TypeError, ValueError):
                    minimum_stop_pct = float('nan')
                actual_stop_pct = risk_distance / entry
                if (not math.isfinite(minimum_stop_pct) or minimum_stop_pct <= 0
                        or actual_stop_pct < minimum_stop_pct):
                    self.last_decision[key] = now
                    self.last_rejection = 'entry_too_close_to_stop'
                    self.audit.event(
                        'SIGNAL_REJECTED', decision_id, user_id=user_id,
                        mode=self.execution_mode, symbol=snapshot.symbol,
                        lifecycle_id=lifecycle_ctx.get("lifecycle_id"), watch_id=lifecycle_ctx.get("watch_id"),
                        setup_id=lifecycle_ctx.get("setup_id"), stage="REJECTED",
                        strategy=getattr(intent.strategy, 'value', str(intent.strategy)),
                        direction=getattr(intent.direction, 'value', str(intent.direction)),
                        reason='entry_too_close_to_stop', entry_price=entry,
                        signal_entry_price=signal_entry, stop_price=stop,
                        target_price=target, actual_stop_pct=actual_stop_pct,
                        minimum_stop_pct=minimum_stop_pct,
                    )
                    self._terminal_lifecycle(
                        user_id, lifecycle_ctx=lifecycle_ctx, symbol=snapshot.symbol, decision_id=decision_id,
                        reason='entry_too_close_to_stop', strategy=intent.strategy, direction=intent.direction,
                        details={"actual_stop_pct": actual_stop_pct},
                    )
                    return None
                if actual_stop_pct > 1.25 * float(planned_stop_pct):
                    self.last_decision[key] = now
                    self.last_rejection = 'entry_far_from_signal'
                    self.audit.event(
                        'SIGNAL_REJECTED', decision_id, user_id=user_id,
                        mode=self.execution_mode, symbol=snapshot.symbol,
                        lifecycle_id=lifecycle_ctx.get("lifecycle_id"), watch_id=lifecycle_ctx.get("watch_id"),
                        setup_id=lifecycle_ctx.get("setup_id"), stage="REJECTED",
                        strategy=getattr(intent.strategy, 'value', str(intent.strategy)),
                        direction=getattr(intent.direction, 'value', str(intent.direction)),
                        reason='entry_far_from_signal', entry_price=entry,
                        signal_entry_price=signal_entry, stop_price=stop,
                        target_price=target, actual_stop_pct=actual_stop_pct,
                        planned_stop_pct=float(planned_stop_pct),
                    )
                    self._terminal_lifecycle(
                        user_id, lifecycle_ctx=lifecycle_ctx, symbol=snapshot.symbol, decision_id=decision_id,
                        reason='entry_far_from_signal', strategy=intent.strategy, direction=intent.direction,
                        details={"actual_stop_pct": actual_stop_pct},
                    )
                    return None

            minimum_execution_rr = (
                max(MIN_EXECUTION_RR, breakout.ARMED_MIN_EXECUTION_RR)
                if armed_breakout
                else MIN_EXECUTION_RR
            )
            if execution_rr < minimum_execution_rr:
                self.last_decision[key] = now
                self.last_rejection = 'execution_rr_too_low'
                self.audit.event(
                    "SIGNAL_REJECTED", decision_id, user_id=user_id,
                    mode=self.execution_mode, symbol=snapshot.symbol,
                    lifecycle_id=lifecycle_ctx.get("lifecycle_id"), watch_id=lifecycle_ctx.get("watch_id"),
                    setup_id=lifecycle_ctx.get("setup_id"), stage="REJECTED",
                    strategy=getattr(intent.strategy, "value", str(intent.strategy)),
                    direction=getattr(intent.direction, "value", str(intent.direction)),
                    reason='execution_rr_too_low', entry_price=entry,
                    stop_price=stop, target_price=target,
                    execution_rr=round(execution_rr, 4), minimum_rr=minimum_execution_rr,
                )
                self._terminal_lifecycle(
                    user_id, lifecycle_ctx=lifecycle_ctx, symbol=snapshot.symbol, decision_id=decision_id,
                    reason='execution_rr_too_low', strategy=intent.strategy, direction=intent.direction,
                    details={"execution_rr": round(execution_rr, 4)},
                )
                return None

            structural_rr = (getattr(intent, "metadata", {}) or {}).get(
                "structural_rr_estimate"
            )
            self.audit.event(
                "SIGNAL_ACCEPTED", decision_id, user_id=user_id,
                mode=self.execution_mode, symbol=snapshot.symbol,
                lifecycle_id=lifecycle_ctx.get("lifecycle_id"), watch_id=lifecycle_ctx.get("watch_id"),
                setup_id=lifecycle_ctx.get("setup_id"), stage="SIGNAL_ACCEPTED",
                stage_order=LIFECYCLE_STAGE_ORDER["SIGNAL_ACCEPTED"],
                strategy=getattr(intent.strategy, "value", str(intent.strategy)),
                direction=getattr(intent.direction, "value", str(intent.direction)),
                quality=round(float(intent.quality), 2), entry_price=entry,
                stop_price=stop, target_price=target,
                execution_rr=round(execution_rr, 4), structural_rr=structural_rr,
                risk_multiplier=intent.risk_multiplier,
            )
            self._schedule_lifecycle_stage(
                user_id, lifecycle_id=lifecycle_ctx.get("lifecycle_id"), stage="SIGNAL_ACCEPTED",
                symbol=snapshot.symbol, strategy=intent.strategy, direction=intent.direction,
                decision_id=decision_id, watch_id=lifecycle_ctx.get("watch_id"),
                setup_id=lifecycle_ctx.get("setup_id"), reason="signal_accepted",
                details={"execution_rr": round(execution_rr, 4), "quality": round(float(intent.quality), 2)},
            )
            accepted_signal = True
            self._funnel("signal_accepted", user_id=user_id, symbol=snapshot.symbol)

            # -------------------------- RISK STAGE --------------------------
            try:
                risk = self.risk.evaluate(
                    intent, equity, leverage=getattr(self.execution, "leverage", 1),
                    available_equity=available_equity,
                )
            except Exception as exc:
                terminal_event_emitted = True
                self.last_decision[key] = now
                self.audit.event(
                    "PIPELINE_ERROR", decision_id, user_id=user_id,
                    mode=self.execution_mode, symbol=snapshot.symbol,
                    lifecycle_id=lifecycle_ctx.get("lifecycle_id"), watch_id=lifecycle_ctx.get("watch_id"),
                    setup_id=lifecycle_ctx.get("setup_id"), stage="risk_evaluate",
                    error=f"{type(exc).__name__}: {exc}",
                )
                self._terminal_lifecycle(
                    user_id, lifecycle_ctx=lifecycle_ctx, symbol=snapshot.symbol, decision_id=decision_id,
                    reason="risk_evaluation_error", strategy=intent.strategy, direction=intent.direction, stage="ERROR",
                )
                return {"accepted": False, "filled": False, "reason": "risk_evaluation_error"}

            self.audit.event(
                "RISK_EVALUATED", decision_id, user_id=user_id,
                mode=self.execution_mode, symbol=snapshot.symbol,
                lifecycle_id=lifecycle_ctx.get("lifecycle_id"), watch_id=lifecycle_ctx.get("watch_id"),
                setup_id=lifecycle_ctx.get("setup_id"), approved=bool(risk.approved), reason=str(risk.reason),
                equity=float(equity), quote_notional=float(risk.quantity),
                base_quantity=float(risk.base_quantity),
                margin_required=float(risk.margin_required), leverage=int(risk.leverage),
            )

            if not risk.approved:
                self._funnel("risk_rejected", str(risk.reason), user_id=user_id, symbol=snapshot.symbol)
                terminal_event_emitted = True
                self.last_decision[key] = now
                self.audit.event(
                    "RISK_REJECTED", decision_id, user_id=user_id,
                    mode=self.execution_mode, symbol=snapshot.symbol,
                    lifecycle_id=lifecycle_ctx.get("lifecycle_id"), watch_id=lifecycle_ctx.get("watch_id"),
                    setup_id=lifecycle_ctx.get("setup_id"), stage="REJECTED",
                    strategy=getattr(intent.strategy, "value", str(intent.strategy)),
                    direction=getattr(intent.direction, "value", str(intent.direction)),
                    reason=str(risk.reason),
                )
                self._terminal_lifecycle(
                    user_id, lifecycle_ctx=lifecycle_ctx, symbol=snapshot.symbol, decision_id=decision_id,
                    reason=f"risk:{risk.reason}", strategy=intent.strategy, direction=intent.direction,
                )
                self.persistence.schedule_decision(
                    decision_id=decision_id, user_id=user_id, mode=self.execution_mode,
                    symbol=snapshot.symbol,
                    document=self._decision_doc(
                        snapshot, intent, risk, status="RISK_REJECTED",
                        execution_rr=execution_rr, structural_rr=structural_rr,
                    ),
                )
                return {"accepted": False, "filled": False, "reason": str(risk.reason)}

            self._funnel("risk_approved", user_id=user_id, symbol=snapshot.symbol)
            self.audit.event(
                "RISK_APPROVED", decision_id, user_id=user_id, mode=self.execution_mode,
                symbol=snapshot.symbol, lifecycle_id=lifecycle_ctx.get("lifecycle_id"),
                watch_id=lifecycle_ctx.get("watch_id"), setup_id=lifecycle_ctx.get("setup_id"),
                stage="RISK_APPROVED", stage_order=LIFECYCLE_STAGE_ORDER["RISK_APPROVED"],
                strategy=getattr(intent.strategy, "value", str(intent.strategy)),
                direction=getattr(intent.direction, "value", str(intent.direction)),
                quote_notional=float(risk.quantity), base_quantity=float(risk.base_quantity),
                margin_required=float(risk.margin_required), leverage=int(risk.leverage),
            )
            self._schedule_lifecycle_stage(
                user_id, lifecycle_id=lifecycle_ctx.get("lifecycle_id"), stage="RISK_APPROVED",
                symbol=snapshot.symbol, strategy=intent.strategy, direction=intent.direction,
                decision_id=decision_id, watch_id=lifecycle_ctx.get("watch_id"),
                setup_id=lifecycle_ctx.get("setup_id"), reason="risk_approved",
                details={"leverage": int(risk.leverage), "margin_required": float(risk.margin_required)},
            )
            # ----------------------- EXECUTION STAGE ------------------------
            # No Mongo/Telegram work has run between SIGNAL_ACCEPTED and here.
            try:
                market_bid = float(snapshot.bid)
                market_ask = float(snapshot.ask)
            except (TypeError, ValueError):
                market_bid = 0.0
                market_ask = 0.0

            quote_ms = getattr(snapshot, 'quote_received_ms', now_ms)
            execution_now_ms = int(time.time() * 1000)
            try:
                quote_age_ms = max(0, execution_now_ms - int(quote_ms))
            except (TypeError, ValueError):
                quote_age_ms = None
            if (not math.isfinite(market_bid) or not math.isfinite(market_ask)
                    or market_bid <= 0 or market_ask <= 0 or market_bid > market_ask
                    or quote_age_ms is None or quote_age_ms > 10_000):
                self.last_rejection = 'market_unavailable'
                self._funnel("execution_rejected", "market_unavailable", user_id=user_id, symbol=snapshot.symbol)
                result = {"accepted": False, "filled": False, "reason": "market_unavailable"}
                terminal_event_emitted = True
                self.last_decision[key] = now
                self.audit.event(
                    "EXECUTION_REJECTED", decision_id, user_id=user_id,
                    mode=self.execution_mode, symbol=snapshot.symbol,
                    lifecycle_id=lifecycle_ctx.get("lifecycle_id"), watch_id=lifecycle_ctx.get("watch_id"),
                    setup_id=lifecycle_ctx.get("setup_id"), stage="REJECTED",
                    strategy=getattr(intent.strategy, "value", str(intent.strategy)),
                    direction=getattr(intent.direction, "value", str(intent.direction)),
                    reason="market_unavailable",
                    orderbook_valid=getattr(snapshot, "orderbook_valid", False),
                    market_bid=market_bid, market_ask=market_ask,
                    quote_received_ms=quote_ms, quote_age_ms=quote_age_ms,
                )
                self._terminal_lifecycle(
                    user_id, lifecycle_ctx=lifecycle_ctx, symbol=snapshot.symbol, decision_id=decision_id,
                    reason="market_unavailable", strategy=intent.strategy, direction=intent.direction,
                )
                self.persistence.schedule_decision(
                    decision_id=decision_id, user_id=user_id, mode=self.execution_mode,
                    symbol=snapshot.symbol,
                    document=self._decision_doc(
                        snapshot, intent, risk, status="EXECUTION_REJECTED",
                        execution_rr=execution_rr, structural_rr=structural_rr,
                        execution=result,
                    ),
                )
                return result

            executable_price = market_ask if intent.direction == Direction.LONG else market_bid
            if not ((intent.direction == Direction.LONG and stop < executable_price < target)
                    or (intent.direction == Direction.SHORT and target < executable_price < stop)):
                terminal_event_emitted = True
                self.last_rejection = 'fill_outside_trade_geometry'
                self._funnel("execution_rejected", "fill_outside_trade_geometry", user_id=user_id, symbol=snapshot.symbol)
                self.audit.event('EXECUTION_REJECTED', decision_id, user_id=user_id,
                                 mode=self.execution_mode, symbol=snapshot.symbol,
                                 lifecycle_id=lifecycle_ctx.get("lifecycle_id"), watch_id=lifecycle_ctx.get("watch_id"),
                                 setup_id=lifecycle_ctx.get("setup_id"), stage="REJECTED",
                                 strategy=getattr(intent.strategy, "value", str(intent.strategy)),
                                 direction=getattr(intent.direction, "value", str(intent.direction)),
                                 reason='fill_outside_trade_geometry')
                self._terminal_lifecycle(
                    user_id, lifecycle_ctx=lifecycle_ctx, symbol=snapshot.symbol, decision_id=decision_id,
                    reason='fill_outside_trade_geometry', strategy=intent.strategy, direction=intent.direction,
                )
                return {'accepted': False, 'filled': False, 'reason': 'fill_outside_trade_geometry'}
            if self.entry_guard is not None and not self.entry_guard():
                raise RuntimeError('trading_worker_lease_expired')
            armed_setup_id = str((getattr(intent, 'metadata', {}) or {}).get('armed_setup_id') or '')
            claim_id = None
            if armed_setup_id:
                # Durable idempotency for priority-triggered setups. This survives a
                # worker restart even when the priority snapshot has no 5m candles.
                claim_id = f'{user_id}:{self.execution_mode}:armed:{armed_setup_id}'
            elif snapshot.candles:
                bar_ts = snapshot.candles[-1].timestamp
                claim_id = f'{user_id}:{self.execution_mode}:{snapshot.symbol}:{bar_ts}'
            if claim_id:
                claim = await asyncio.to_thread(self.db.set_once, 'signal_claims', {'claim_id': claim_id}, {
                    'decision_id': decision_id, 'expires_at': datetime.now(timezone.utc) + timedelta(days=7),
                })
                if claim['decision_id'] != decision_id:
                    terminal_event_emitted = True
                    self.last_rejection = 'setup_already_executed'
                    self._funnel("execution_rejected", "setup_already_executed", user_id=user_id, symbol=snapshot.symbol)
                    self.audit.event('EXECUTION_REJECTED', decision_id, user_id=user_id,
                                     mode=self.execution_mode, symbol=snapshot.symbol,
                                     lifecycle_id=lifecycle_ctx.get("lifecycle_id"), watch_id=lifecycle_ctx.get("watch_id"),
                                     setup_id=lifecycle_ctx.get("setup_id"), stage="REJECTED",
                                     strategy=getattr(intent.strategy, "value", str(intent.strategy)),
                                     direction=getattr(intent.direction, "value", str(intent.direction)),
                                     reason='setup_already_executed')
                    self._terminal_lifecycle(
                        user_id, lifecycle_ctx=lifecycle_ctx, symbol=snapshot.symbol, decision_id=decision_id,
                        reason='setup_already_executed', strategy=intent.strategy, direction=intent.direction,
                    )
                    return {'accepted': False, 'filled': False, 'reason': 'setup_already_executed'}
            if self.execution_mode == 'live':
                reservation = {'user_id': user_id, 'active': True, 'symbol': snapshot.symbol,
                               'decision_id': decision_id, 'order_id': None, 'created_ms': now_ms}
                await asyncio.to_thread(self.db.upsert, 'execution_pending', {'user_id': user_id}, reservation)
                self.pending_execution = {**reservation, 'created_monotonic': time.monotonic()}
            # Claims/reservation also await storage. Never submit if that wait has
            # invalidated the checked quote, and never stamp a stale quote as fresh.
            final_quote_age_ms = int(time.time() * 1000) - int(quote_ms)
            if final_quote_age_ms < 0 or final_quote_age_ms > 10_000:
                if self.execution_mode == "live":
                    await self._clear_pending(user_id)
                self.last_rejection = "execution_quote_expired_before_submit"
                terminal_event_emitted = True
                self._funnel("execution_rejected", self.last_rejection, user_id=user_id, symbol=snapshot.symbol)
                self.audit.event(
                    "EXECUTION_REJECTED", decision_id, user_id=user_id, mode=self.execution_mode,
                    symbol=snapshot.symbol, reason=self.last_rejection, quote_age_ms=final_quote_age_ms,
                    lifecycle_id=lifecycle_ctx.get("lifecycle_id"), stage="REJECTED",
                )
                self._terminal_lifecycle(
                    user_id, lifecycle_ctx=lifecycle_ctx, symbol=snapshot.symbol, decision_id=decision_id,
                    reason=self.last_rejection, strategy=intent.strategy, direction=intent.direction,
                )
                return {"accepted": False, "filled": False, "reason": self.last_rejection}
            if self.entry_guard is not None and not self.entry_guard():
                raise RuntimeError('trading_worker_lease_expired')
            self._funnel("submitted", user_id=user_id, symbol=snapshot.symbol)
            self.audit.event(
                "ORDER_SUBMITTED", decision_id, user_id=user_id, mode=self.execution_mode,
                symbol=snapshot.symbol, lifecycle_id=lifecycle_ctx.get("lifecycle_id"),
                watch_id=lifecycle_ctx.get("watch_id"), setup_id=lifecycle_ctx.get("setup_id"),
                stage="ORDER_SUBMITTED", stage_order=LIFECYCLE_STAGE_ORDER["ORDER_SUBMITTED"],
                strategy=getattr(intent.strategy, "value", str(intent.strategy)),
                direction=getattr(intent.direction, "value", str(intent.direction)),
                quantity=float(risk.quantity), executable_price=executable_price,
            )
            self._schedule_lifecycle_stage(
                user_id, lifecycle_id=lifecycle_ctx.get("lifecycle_id"), stage="ORDER_SUBMITTED",
                symbol=snapshot.symbol, strategy=intent.strategy, direction=intent.direction,
                decision_id=decision_id, watch_id=lifecycle_ctx.get("watch_id"),
                setup_id=lifecycle_ctx.get("setup_id"), reason="order_submitted",
                details={"quantity": float(risk.quantity), "executable_price": executable_price},
            )
            try:
                result = self.execution.submit(
                    intent,
                    risk.quantity,
                    {"bid": market_bid, "ask": market_ask, "last": snapshot.last, "ts": now_ms},
                )
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:
                terminal_event_emitted = True
                self.last_decision[key] = now
                self._funnel("execution_error", "execution_submit_error", user_id=user_id, symbol=snapshot.symbol)
                self.audit.event(
                    "PIPELINE_ERROR", decision_id, user_id=user_id,
                    mode=self.execution_mode, symbol=snapshot.symbol,
                    lifecycle_id=lifecycle_ctx.get("lifecycle_id"), watch_id=lifecycle_ctx.get("watch_id"),
                    setup_id=lifecycle_ctx.get("setup_id"), stage="execution_submit",
                    error=f"{type(exc).__name__}: {exc}",
                )
                self._terminal_lifecycle(
                    user_id, lifecycle_ctx=lifecycle_ctx, symbol=snapshot.symbol, decision_id=decision_id,
                    reason="execution_error", strategy=intent.strategy, direction=intent.direction, stage="ERROR",
                )
                return {"accepted": False, "filled": False, "reason": "execution_error"}

            if not isinstance(result, dict):
                result = {"accepted": False, "filled": False, "reason": "invalid_execution_result"}

            # Never splat execution context keys (mode/symbol/user_id/decision_id)
            # into AuditLogger together with the canonical context arguments.
            # PaperExecutionEngine intentionally returns ``mode=demo`` and the old
            # call raised ``TypeError: ... got multiple values for keyword
            # argument 'mode'`` immediately after a successful fill. That was a
            # real SIGNAL_ACCEPTED -> no POSITION_OPENED pipeline cut.
            execution_log = {
                k: v for k, v in result.items()
                if k not in {
                    "position", "exchange_response", "order_state",
                    "mode", "symbol", "user_id", "decision_id",
                }
            }
            self.audit.event(
                "EXECUTION_RESULT", decision_id, user_id=user_id,
                mode=self.execution_mode, symbol=snapshot.symbol,
                lifecycle_id=lifecycle_ctx.get("lifecycle_id"), watch_id=lifecycle_ctx.get("watch_id"),
                setup_id=lifecycle_ctx.get("setup_id"), **execution_log,
            )

            if not result.get("filled"):
                reason = str(result.get("reason") or "not_filled")
                self._funnel("execution_rejected", reason, user_id=user_id, symbol=snapshot.symbol)
                pending = bool(result.get("accepted"))
                event = "EXECUTION_PENDING" if pending else "EXECUTION_REJECTED"
                terminal_event_emitted = True
                self.last_decision[key] = now
                lifecycle_stage = "EXECUTION_PENDING" if pending else "REJECTED"
                self.audit.event(
                    event, decision_id, user_id=user_id, mode=self.execution_mode,
                    symbol=snapshot.symbol, lifecycle_id=lifecycle_ctx.get("lifecycle_id"),
                    watch_id=lifecycle_ctx.get("watch_id"), setup_id=lifecycle_ctx.get("setup_id"),
                    stage=lifecycle_stage, stage_order=LIFECYCLE_STAGE_ORDER.get(lifecycle_stage),
                    strategy=getattr(intent.strategy, "value", str(intent.strategy)),
                    direction=getattr(intent.direction, "value", str(intent.direction)), reason=reason,
                    accepted=bool(result.get("accepted", False)), order_id=result.get("order_id"),
                )
                if pending:
                    self._schedule_lifecycle_stage(
                        user_id, lifecycle_id=lifecycle_ctx.get("lifecycle_id"), stage="EXECUTION_PENDING",
                        symbol=snapshot.symbol, strategy=intent.strategy, direction=intent.direction,
                        decision_id=decision_id, watch_id=lifecycle_ctx.get("watch_id"),
                        setup_id=lifecycle_ctx.get("setup_id"), reason=reason,
                        details={"order_id": result.get("order_id")},
                    )
                else:
                    self._terminal_lifecycle(
                        user_id, lifecycle_ctx=lifecycle_ctx, symbol=snapshot.symbol, decision_id=decision_id,
                        reason=reason, strategy=intent.strategy, direction=intent.direction,
                    )
                if not pending and self.execution_mode == 'live':
                    await self._clear_pending(user_id)
                if pending:
                    self.pending_execution = {
                        "decision_id": decision_id,
                        "symbol": snapshot.symbol,
                        "order_id": result.get("order_id"),
                        "intent": intent,
                        "risk": risk,
                        "execution_rr": round(execution_rr, 4),
                        "structural_rr": structural_rr,
                        "created_monotonic": time.monotonic(),
                    }
                    if self.execution_mode == 'live':
                        await asyncio.to_thread(self.db.upsert, 'execution_pending', {'user_id': user_id},
                                                {'active': True, 'order_id': result.get('order_id')})
                # Post-submit persistence is allowed here because the execution
                # outcome is already known and visible.
                if result.get("order_id"):
                    ok, err = await self.persistence.save_order_state(
                        decision_id=decision_id, user_id=user_id,
                        mode=self.execution_mode, result=result,
                    )
                    if not ok:
                        self.audit.event(
                            "ORDER_PERSIST_ERROR", decision_id, level="ERROR",
                            user_id=user_id, mode=self.execution_mode,
                            symbol=snapshot.symbol, error=err,
                        )
                self.persistence.schedule_decision(
                    decision_id=decision_id, user_id=user_id, mode=self.execution_mode,
                    symbol=snapshot.symbol,
                    document=self._decision_doc(
                        snapshot, intent, risk, status=event,
                        execution_rr=execution_rr, structural_rr=structural_rr,
                        execution=result,
                    ),
                )
                return result

            if result.get("filled") and not result.get("position"):
                terminal_event_emitted = True
                self.last_decision[key] = now
                self._funnel("execution_error", "filled_result_missing_position", user_id=user_id, symbol=snapshot.symbol)
                self.audit.event(
                    "PIPELINE_ERROR", decision_id, user_id=user_id,
                    mode=self.execution_mode, symbol=snapshot.symbol,
                    lifecycle_id=lifecycle_ctx.get("lifecycle_id"), watch_id=lifecycle_ctx.get("watch_id"),
                    setup_id=lifecycle_ctx.get("setup_id"), stage="execution_result",
                    error="filled_result_missing_position",
                )
                self._terminal_lifecycle(
                    user_id, lifecycle_ctx=lifecycle_ctx, symbol=snapshot.symbol, decision_id=decision_id,
                    reason="filled_result_missing_position", strategy=intent.strategy, direction=intent.direction, stage="ERROR",
                )
                return {**result, "filled": False, "reason": "filled_result_missing_position"}

            # ---------------------- FINALIZATION STAGE ----------------------
            position = self._position_from_result(result["position"], intent)
            strategy_name = getattr(intent.strategy, "value", str(intent.strategy))
            position.protected = bool(result.get("protected", True))
            position.strategy = strategy_name
            position.quality = round(float(intent.quality), 2)
            position.execution_rr = round(execution_rr, 4)
            position.lifecycle_id = lifecycle_ctx.get("lifecycle_id")
            position.watch_id = lifecycle_ctx.get("watch_id")
            position.setup_id = lifecycle_ctx.get("setup_id")
            if structural_rr is not None:
                position.structural_rr = structural_rr

            # Block duplicates immediately in memory. Persistence is bounded and
            # cannot turn an actual fill into a fake rejection.
            self.pending_execution = None
            self.position_manager.add(position, persist=False)

            persisted, persist_error = await self.persistence.save_fill(
                decision_id=decision_id,
                user_id=user_id,
                mode=self.execution_mode,
                position=position,
                execution_result=result,
            )
            if not persisted:
                self.audit.event(
                    "POSITION_PERSIST_ERROR", decision_id, level="ERROR",
                    user_id=user_id, mode=self.execution_mode, symbol=position.symbol,
                    position_id=position.position_id, error=persist_error,
                )
                self.persistence.schedule_fill_retry(
                    decision_id=decision_id, user_id=user_id, mode=self.execution_mode,
                    symbol=position.symbol, position=position, execution_result=result,
                )
            elif persist_error:
                self.audit.event(
                    "ORDER_PERSIST_ERROR", decision_id, level="ERROR",
                    user_id=user_id, mode=self.execution_mode, symbol=position.symbol,
                    position_id=position.position_id, error=persist_error,
                )

            if persisted and self.execution_mode == 'live':
                await self._clear_pending(user_id)
            self.last_rejection = None
            self._funnel("filled", user_id=user_id, symbol=snapshot.symbol)
            terminal_event_emitted = True
            self.last_decision[key] = now
            self.audit.event(
                "POSITION_OPENED", decision_id, user_id=user_id,
                mode=self.execution_mode, symbol=position.symbol,
                lifecycle_id=lifecycle_ctx.get("lifecycle_id"), watch_id=lifecycle_ctx.get("watch_id"),
                setup_id=lifecycle_ctx.get("setup_id"), position_id=position.position_id,
                stage="POSITION_OPENED", stage_order=LIFECYCLE_STAGE_ORDER["POSITION_OPENED"],
                lifecycle_complete=True,
                direction=getattr(position.direction, "value", str(position.direction)),
                quantity=position.quantity, entry_price=position.entry_price,
                stop_price=position.stop_price, target_price=position.target_price,
                strategy=strategy_name, execution_rr=round(execution_rr, 4),
                persisted=bool(persisted), protected=result.get("protected"),
                source="execution",
            )
            self._schedule_lifecycle_stage(
                user_id, lifecycle_id=lifecycle_ctx.get("lifecycle_id"), stage="POSITION_OPENED",
                symbol=position.symbol, strategy=intent.strategy, direction=intent.direction,
                decision_id=decision_id, watch_id=lifecycle_ctx.get("watch_id"),
                setup_id=lifecycle_ctx.get("setup_id"), position_id=position.position_id,
                reason="position_opened", terminal=True,
                details={"entry_price": position.entry_price, "execution_rr": round(execution_rr, 4)},
            )
            await self._notify_opened(position, decision_id, user_id)

            self.persistence.schedule_decision(
                decision_id=decision_id, user_id=user_id, mode=self.execution_mode,
                symbol=snapshot.symbol,
                document=self._decision_doc(
                    snapshot, intent, risk, status="POSITION_OPENED",
                    execution_rr=execution_rr, structural_rr=structural_rr,
                    execution=result,
                ),
            )
            return result

        except asyncio.CancelledError:
            if accepted_signal and not terminal_event_emitted:
                terminal_event_emitted = True
                self._funnel("execution_error", "pipeline_cancelled_after_signal_accept", user_id=user_id, symbol=getattr(snapshot, "symbol", None))
                self.audit.event(
                    "PIPELINE_ERROR", decision_id, user_id=user_id,
                    mode=self.execution_mode, symbol=getattr(snapshot, "symbol", None),
                    lifecycle_id=lifecycle_ctx.get("lifecycle_id"), watch_id=lifecycle_ctx.get("watch_id"),
                    setup_id=lifecycle_ctx.get("setup_id"), stage="cancelled",
                    error="pipeline_cancelled_after_signal_accept",
                )
                self._terminal_lifecycle(
                    user_id, lifecycle_ctx=lifecycle_ctx, symbol=getattr(snapshot, "symbol", None),
                    decision_id=decision_id, reason="pipeline_cancelled_after_signal_accept", stage="ERROR",
                )
            raise
        except Exception as exc:
            if accepted_signal and not terminal_event_emitted:
                terminal_event_emitted = True
                execution_error_reason = (
                    "trading_worker_lease_expired"
                    if str(exc) == "trading_worker_lease_expired"
                    else f"orchestrator_unhandled:{type(exc).__name__}"
                )
                self._funnel("execution_error", execution_error_reason, user_id=user_id, symbol=getattr(snapshot, "symbol", None))
                self.audit.event(
                    "PIPELINE_ERROR", decision_id, user_id=user_id,
                    mode=self.execution_mode, symbol=getattr(snapshot, "symbol", None),
                    lifecycle_id=lifecycle_ctx.get("lifecycle_id"), watch_id=lifecycle_ctx.get("watch_id"),
                    setup_id=lifecycle_ctx.get("setup_id"), stage="orchestrator_unhandled",
                    error=f"{type(exc).__name__}: {exc}",
                )
                self._terminal_lifecycle(
                    user_id, lifecycle_ctx=lifecycle_ctx, symbol=getattr(snapshot, "symbol", None),
                    decision_id=decision_id, reason="orchestrator_unhandled", stage="ERROR",
                )
            else:
                self.audit.event(
                    "DECISION_ERROR", decision_id, level="ERROR", user_id=user_id,
                    mode=self.execution_mode, symbol=getattr(snapshot, "symbol", None),
                    error=f"{type(exc).__name__}: {exc}",
                )
            self.last_decision[key] = now
            return {"accepted": bool(accepted_signal), "filled": False, "reason": "pipeline_error"}
        finally:
            if accepted_signal and not terminal_event_emitted:
                self._funnel("execution_error", "accepted_signal_without_terminal_event", user_id=user_id, symbol=getattr(snapshot, "symbol", None))
                self.audit.event(
                    "PIPELINE_ERROR", decision_id, user_id=user_id,
                    mode=self.execution_mode, symbol=getattr(snapshot, "symbol", None),
                    lifecycle_id=lifecycle_ctx.get("lifecycle_id"), watch_id=lifecycle_ctx.get("watch_id"),
                    setup_id=lifecycle_ctx.get("setup_id"), stage="terminal_guard",
                    error="accepted_signal_without_terminal_event",
                )
                self._terminal_lifecycle(
                    user_id, lifecycle_ctx=lifecycle_ctx, symbol=getattr(snapshot, "symbol", None),
                    decision_id=decision_id, reason="accepted_signal_without_terminal_event", stage="ERROR",
                )
            self.inflight.discard(key)

    async def _clear_pending(self, user_id):
        if self.execution_mode == 'live':
            await asyncio.to_thread(self.db.upsert, 'execution_pending', {'user_id': user_id}, {'active': False})
        self.pending_execution = None
