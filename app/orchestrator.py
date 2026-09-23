from __future__ import annotations

import asyncio
import inspect
import math
import time
from uuid import uuid4

from app.models.enums import Direction
from app.models.trading import Position
from app.trading.persistence import TradePersistence


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
                 persistence=None):
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

    def _has_open_position(self, symbol=None, user_id=None):
        # Source-bot invariant: one open trade at a time per user/runtime.
        # The old expression used ``user_id is not None or p.symbol == symbol``;
        # because user_id is always present in production it was easy to misread
        # and impossible to reason about. Keep the intended invariant explicit.
        return any(p.status == "OPEN" for p in self.position_manager.positions.values())

    @staticmethod
    def _position_from_result(raw, intent) -> Position:
        if isinstance(raw, Position):
            return raw
        direction = raw.get("direction", intent.direction)
        if not isinstance(direction, Direction):
            direction = Direction(str(direction))
        return Position(
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
            opened_at=raw.get("opened_at"),
            closed_at=raw.get("closed_at"),
            exit_price=raw.get("exit_price"),
            exit_reason=raw.get("exit_reason"),
        )

    def _decision_doc(self, snapshot, intent, risk, *, status, execution_rr,
                      structural_rr, execution=None):
        doc = {
            "symbol": str(snapshot.symbol),
            "timeframe": str(snapshot.timeframe),
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

    async def on_snapshot(self, snapshot, equity, user_id=None, allow_entries=True):
        now_ms = int(time.time() * 1000)

        # LIVE reconciliation runs even while new entries are paused. This is what
        # lets a delayed exchange fill or externally closed position converge back
        # into KAELEON state.
        if hasattr(self.execution, "sync_symbol"):
            try:
                exchange_positions = await self.execution.sync_symbol(snapshot.symbol)
                changes = self.position_manager.reconcile_exchange(
                    exchange_positions, now_ms, symbol=snapshot.symbol,
                    persist=False, notify=False,
                )
                for position in changes.get("updated", []):
                    self.persistence.schedule_position_save(
                        position=position, user_id=user_id, mode=self.execution_mode,
                        symbol=position.symbol,
                    )
                for position in changes.get("closed", []):
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
                            position.strategy = getattr(intent.strategy, "value", str(intent.strategy))
                            position.quality = round(float(intent.quality), 2)
                            position.execution_rr = pending.get("execution_rr")
                            if pending.get("structural_rr") is not None:
                                position.structural_rr = pending.get("structural_rr")
                        self.pending_execution = None
                    self.persistence.schedule_position_save(
                        position=position, user_id=user_id, mode=self.execution_mode,
                        symbol=position.symbol,
                    )
                    await self._notify_opened(position, position.decision_id, user_id)
            except Exception as exc:
                self.audit.event(
                    "POSITION_SYNC_ERROR", user_id, user_id=user_id,
                    mode=self.execution_mode, symbol=snapshot.symbol,
                    error=f"{type(exc).__name__}: {exc}",
                )

        if snapshot.last:
            price = snapshot.last if isinstance(snapshot.last, (int, float)) else snapshot.last.close
            self.position_manager.mark(snapshot.symbol, price, now_ms)

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
                    self.pending_execution = None
                elif order_status == "finish" and age >= 15.0:
                    # A finished order with no current position is terminal at the
                    # exchange (for example, it may have opened and closed rapidly).
                    # Do not keep the account permanently locked.
                    self.audit.event(
                        "EXECUTION_PENDING_CLEARED", pending.get("decision_id"),
                        user_id=user_id, mode=self.execution_mode, symbol=pending.get("symbol"),
                        reason="order_finished_without_open_position",
                        order_id=pending.get("order_id"),
                    )
                    self.pending_execution = None
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
        if not allow_entries:
            self.audit.event(
                "ENTRY_SKIPPED", user_id, level="DEBUG", persist=False,
                user_id=user_id, mode=self.execution_mode, symbol=snapshot.symbol,
                reason="entries_disabled",
            )
            return None

        key = (snapshot.symbol, snapshot.timeframe)
        now = time.time()
        if now - self.last_decision.get(key, 0) < self.cooldown_seconds:
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

        self.audit.event(
            "DECISION_START", decision_id, level="DEBUG", persist=False,
            user_id=user_id, mode=self.execution_mode, symbol=snapshot.symbol,
            timeframe=snapshot.timeframe, equity=equity,
            data_complete=getattr(snapshot, "data_complete", None),
            orderbook_valid=getattr(snapshot, "orderbook_valid", None),
        )

        try:
            # ------------------------- SIGNAL STAGE -------------------------
            regime = self.regime_engine.evaluate_snapshot(snapshot)
            regime_meta = getattr(self.regime_engine, "last_metadata", {}) or {}
            rf = regime_meta.get("features") or {}
            rs = regime_meta.get("state") or {}
            feature_summary = {
                k: rf.get(k) for k in (
                    "adx", "choppiness", "efficiency_ratio", "atr_pct",
                    "wick_instability", "body_quality", "breakout_failure_ratio",
                    "ema_stack_alignment", "trend_bias", "btc_shock_ratio",
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
                features=feature_summary, state_machine=state_summary,
                breakout_allowed=regime.breakout_allowed, sweep_allowed=regime.sweep_allowed,
                hard_block=regime.hard_block, risk_multiplier=regime.risk_multiplier,
            )

            router_kwargs = {"snapshot": snapshot}
            try:
                router_params = inspect.signature(self.router.evaluate).parameters.values()
                if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in router_params) or any(
                    p.name == "regime_metadata" for p in router_params
                ):
                    router_kwargs["regime_metadata"] = regime_meta
            except (TypeError, ValueError):
                pass
            intent = self.router.evaluate(
                regime, snapshot.candles, decision_id, snapshot.symbol,
                snapshot.timeframe, snapshot.last, **router_kwargs,
            )
            strategy_trace = getattr(self.router, "last_trace", {}) or {}
            compact_trace = {
                "selected": strategy_trace.get("selected"),
                "reason": strategy_trace.get("reason"),
            }
            for name in ("breakout", "sweep"):
                branch = strategy_trace.get(name)
                if isinstance(branch, dict):
                    compact_trace[name] = {
                        k: branch.get(k)
                        for k in ("accepted", "reason", "score", "side", "direction")
                        if k in branch
                    }
            self.audit.event(
                "STRATEGY_EVALUATED", decision_id, user_id=user_id,
                mode=self.execution_mode, symbol=snapshot.symbol,
                regime=regime_meta.get("active"), trace=compact_trace,
            )

            if not intent:
                self.last_decision[key] = now
                rejection_reason = "no_valid_setup"
                for name in ("breakout", "sweep"):
                    branch = strategy_trace.get(name)
                    if isinstance(branch, dict) and branch.get("reason") not in (
                        None, "not_run", "regime_breakout_not_allowed",
                        "regime_sweep_not_allowed",
                    ):
                        rejection_reason = str(branch.get("reason"))
                        break
                self.audit.event(
                    "SIGNAL_REJECTED", decision_id, user_id=user_id,
                    mode=self.execution_mode, symbol=snapshot.symbol,
                    regime=regime_meta.get("active"), reason=rejection_reason,
                )
                return None

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
                self.audit.event(
                    "SIGNAL_REJECTED", decision_id, user_id=user_id,
                    mode=self.execution_mode, symbol=snapshot.symbol,
                    strategy=getattr(intent.strategy, "value", str(intent.strategy)),
                    reason="invalid_trade_geometry", entry_price=entry,
                    stop_price=stop, target_price=target, execution_rr=execution_rr,
                )
                return None

            # A valid strategy setup is not yet executable if CoinW has not
            # supplied both sides of a real order book. Fail closed *before*
            # SIGNAL_ACCEPTED: that event should represent a signal ready to
            # pass through risk and execution, not a candle-only candidate.
            try:
                quote_bid = float(snapshot.bid)
                quote_ask = float(snapshot.ask)
            except (TypeError, ValueError, OverflowError):
                quote_bid, quote_ask = 0.0, 0.0
            quote_valid = (
                math.isfinite(quote_bid) and math.isfinite(quote_ask)
                and quote_bid > 0 and quote_ask > quote_bid
                and getattr(snapshot, "orderbook_valid", True) is not False
            )
            if not quote_valid:
                self.last_decision[key] = now
                self.audit.event(
                    "MARKET_DATA_SKIPPED", decision_id, user_id=user_id,
                    mode=self.execution_mode, symbol=snapshot.symbol,
                    strategy=getattr(intent.strategy, "value", str(intent.strategy)),
                    reason="invalid_orderbook",
                    orderbook_valid=getattr(snapshot, "orderbook_valid", None),
                )
                return {"accepted": False, "filled": False, "reason": "invalid_orderbook"}

            structural_rr = (getattr(intent, "metadata", {}) or {}).get(
                "structural_rr_estimate"
            )
            self.audit.event(
                "SIGNAL_ACCEPTED", decision_id, user_id=user_id,
                mode=self.execution_mode, symbol=snapshot.symbol,
                strategy=getattr(intent.strategy, "value", str(intent.strategy)),
                direction=getattr(intent.direction, "value", str(intent.direction)),
                quality=round(float(intent.quality), 2), entry_price=entry,
                stop_price=stop, target_price=target,
                execution_rr=round(execution_rr, 4), structural_rr=structural_rr,
                risk_multiplier=intent.risk_multiplier,
            )
            accepted_signal = True

            # -------------------------- RISK STAGE --------------------------
            try:
                risk = self.risk.evaluate(
                    intent, equity, leverage=getattr(self.execution, "leverage", 1),
                )
            except Exception as exc:
                terminal_event_emitted = True
                self.last_decision[key] = now
                self.audit.event(
                    "PIPELINE_ERROR", decision_id, user_id=user_id,
                    mode=self.execution_mode, symbol=snapshot.symbol,
                    stage="risk_evaluate", error=f"{type(exc).__name__}: {exc}",
                )
                return {"accepted": False, "filled": False, "reason": "risk_evaluation_error"}

            self.audit.event(
                "RISK_EVALUATED", decision_id, user_id=user_id,
                mode=self.execution_mode, symbol=snapshot.symbol,
                approved=bool(risk.approved), reason=str(risk.reason),
                equity=float(equity), quote_notional=float(risk.quantity),
                base_quantity=float(risk.base_quantity),
                margin_required=float(risk.margin_required), leverage=int(risk.leverage),
            )

            if not risk.approved:
                terminal_event_emitted = True
                self.last_decision[key] = now
                self.audit.event(
                    "RISK_REJECTED", decision_id, user_id=user_id,
                    mode=self.execution_mode, symbol=snapshot.symbol,
                    reason=str(risk.reason),
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

            # ----------------------- EXECUTION STAGE ------------------------
            # No Mongo/Telegram work has run between SIGNAL_ACCEPTED and here.
            # Keep the market guard as a second fail-closed safety boundary.
            # The coordinator normally supplies this validated snapshot; never
            # silently substitute last candle price when book data is missing.
            try:
                market_bid = float(snapshot.bid)
                market_ask = float(snapshot.ask)
            except (TypeError, ValueError, OverflowError):
                market_bid = 0.0
                market_ask = 0.0

            if (not math.isfinite(market_bid) or not math.isfinite(market_ask)
                    or market_bid <= 0 or market_ask <= market_bid
                    or getattr(snapshot, "orderbook_valid", True) is False):
                result = {"accepted": False, "filled": False, "reason": "market_unavailable"}
                terminal_event_emitted = True
                self.last_decision[key] = now
                self.audit.event(
                    "EXECUTION_REJECTED", decision_id, user_id=user_id,
                    mode=self.execution_mode, symbol=snapshot.symbol,
                    reason="market_unavailable",
                    orderbook_valid=getattr(snapshot, "orderbook_valid", False),
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
                self.audit.event(
                    "PIPELINE_ERROR", decision_id, user_id=user_id,
                    mode=self.execution_mode, symbol=snapshot.symbol,
                    stage="execution_submit", error=f"{type(exc).__name__}: {exc}",
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
                **execution_log,
            )

            if not result.get("filled"):
                reason = str(result.get("reason") or "not_filled")
                pending = bool(result.get("accepted"))
                event = "EXECUTION_PENDING" if pending else "EXECUTION_REJECTED"
                terminal_event_emitted = True
                self.last_decision[key] = now
                self.audit.event(
                    event, decision_id, user_id=user_id, mode=self.execution_mode,
                    symbol=snapshot.symbol, reason=reason,
                    accepted=bool(result.get("accepted", False)),
                    order_id=result.get("order_id"),
                )
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
                self.audit.event(
                    "PIPELINE_ERROR", decision_id, user_id=user_id,
                    mode=self.execution_mode, symbol=snapshot.symbol,
                    stage="execution_result", error="filled_result_missing_position",
                )
                return {**result, "filled": False, "reason": "filled_result_missing_position"}

            # ---------------------- FINALIZATION STAGE ----------------------
            position = self._position_from_result(result["position"], intent)
            strategy_name = getattr(intent.strategy, "value", str(intent.strategy))
            position.strategy = strategy_name
            position.quality = round(float(intent.quality), 2)
            position.execution_rr = round(execution_rr, 4)
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

            terminal_event_emitted = True
            self.last_decision[key] = now
            self.audit.event(
                "POSITION_OPENED", decision_id, user_id=user_id,
                mode=self.execution_mode, symbol=position.symbol,
                position_id=position.position_id,
                direction=getattr(position.direction, "value", str(position.direction)),
                quantity=position.quantity, entry_price=position.entry_price,
                stop_price=position.stop_price, target_price=position.target_price,
                strategy=strategy_name, execution_rr=round(execution_rr, 4),
                persisted=bool(persisted), protected=result.get("protected"),
                source="execution",
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
                self.audit.event(
                    "PIPELINE_ERROR", decision_id, user_id=user_id,
                    mode=self.execution_mode, symbol=getattr(snapshot, "symbol", None),
                    stage="cancelled", error="pipeline_cancelled_after_signal_accept",
                )
            raise
        except Exception as exc:
            if accepted_signal and not terminal_event_emitted:
                terminal_event_emitted = True
                self.audit.event(
                    "PIPELINE_ERROR", decision_id, user_id=user_id,
                    mode=self.execution_mode, symbol=getattr(snapshot, "symbol", None),
                    stage="orchestrator_unhandled",
                    error=f"{type(exc).__name__}: {exc}",
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
                self.audit.event(
                    "PIPELINE_ERROR", decision_id, user_id=user_id,
                    mode=self.execution_mode, symbol=getattr(snapshot, "symbol", None),
                    stage="terminal_guard",
                    error="accepted_signal_without_terminal_event",
                )
            self.inflight.discard(key)
