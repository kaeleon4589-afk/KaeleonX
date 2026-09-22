from uuid import uuid4
import inspect
import time
import math

from app.models.trading import Position
from app.models.enums import Direction


class TradingOrchestrator:
    """Single decision/execution coordinator with duplicate and restart safeguards."""

    def __init__(self, regime_engine, router, risk, execution, db, audit,
                 position_manager, signal_factory, cooldown_seconds=30,
                 execution_mode=None, on_position_opened=None):
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

    def _safe_decision_update(self, decision_id, document, *, user_id=None, symbol=None):
        """Persist compact decision state without allowing telemetry persistence to stop trading.

        The decisions collection is diagnostic. A transient BSON/DB serialization issue must
        never cut an accepted signal before risk/execution.
        """
        try:
            self.db.upsert("decisions", {"decision_id": decision_id}, document)
            return True
        except Exception as exc:
            self.audit.event(
                "DECISION_PERSIST_ERROR", decision_id, level="ERROR",
                user_id=user_id, mode=self.execution_mode, symbol=symbol,
                error=f"{type(exc).__name__}: {exc}",
            )
            return False

    def _has_open_position(self, symbol, user_id=None):
        return any(
            p.status == "OPEN" and (user_id is not None or p.symbol == symbol)
            for p in self.position_manager.positions.values()
        )

    async def on_snapshot(self, snapshot, equity, user_id=None, allow_entries=True):
        now_ms = int(time.time() * 1000)
        if hasattr(self.execution, "sync_symbol"):
            try:
                exchange_positions = await self.execution.sync_symbol(snapshot.symbol)
                self.position_manager.reconcile_exchange(exchange_positions, now_ms)
            except Exception as exc:
                self.audit.event(
                    "POSITION_SYNC_ERROR", user_id, user_id=user_id, mode=self.execution_mode,
                    symbol=snapshot.symbol, error=str(exc),
                )
        if snapshot.last:
            price = snapshot.last if isinstance(snapshot.last, (int, float)) else snapshot.last.close
            self.position_manager.mark(snapshot.symbol, price, now_ms)

        if self._has_open_position(snapshot.symbol, user_id=user_id):
            self.audit.event('ENTRY_SKIPPED', user_id, level='DEBUG', persist=False, user_id=user_id, mode=self.execution_mode, symbol=snapshot.symbol, reason='open_position_exists')
            return None
        if not allow_entries:
            self.audit.event('ENTRY_SKIPPED', user_id, level='DEBUG', persist=False, user_id=user_id, mode=self.execution_mode, symbol=snapshot.symbol, reason='entries_disabled')
            return None

        key = (snapshot.symbol, snapshot.timeframe)
        now = time.time()
        if now - self.last_decision.get(key, 0) < self.cooldown_seconds:
            self.audit.event('ENTRY_SKIPPED', user_id, level='DEBUG', persist=False, user_id=user_id, mode=self.execution_mode, symbol=snapshot.symbol, reason='decision_cooldown', remaining_seconds=round(self.cooldown_seconds-(now-self.last_decision.get(key,0)),2))
            return None
        if key in self.inflight:
            self.audit.event('ENTRY_SKIPPED', user_id, level='DEBUG', persist=False, user_id=user_id, mode=self.execution_mode, symbol=snapshot.symbol, reason='decision_inflight')
            return None
        self.inflight.add(key)

        decision_id = uuid4().hex
        accepted_signal = False
        terminal_event_emitted = False
        self.audit.event(
            "DECISION_START", decision_id, level="DEBUG", persist=False,
            user_id=user_id, mode=self.execution_mode, symbol=snapshot.symbol,
            timeframe=snapshot.timeframe, equity=equity, last_price=getattr(snapshot, "last", None),
            data_complete=getattr(snapshot, "data_complete", None), orderbook_valid=getattr(snapshot, "orderbook_valid", None),
        )
        try:
            regime = self.regime_engine.evaluate_snapshot(snapshot)
            regime_meta = getattr(self.regime_engine, "last_metadata", {}) or {}
            # Keep INFO regime logs intentionally compact.  Full EMA/indicator
            # series are useful for calculation but extremely noisy/costly in Railway.
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
                "REGIME_EVALUATED", decision_id, user_id=user_id, mode=self.execution_mode,
                symbol=snapshot.symbol, state=regime.global_state.value, score=regime.core_score,
                candidate=regime_meta.get("candidate"), active=regime_meta.get("active"),
                confidence=regime_meta.get("confidence"), scores=regime_meta.get("scores"),
                features=feature_summary, state_machine=state_summary,
                breakout_allowed=regime.breakout_allowed, sweep_allowed=regime.sweep_allowed,
                hard_block=regime.hard_block, risk_multiplier=regime.risk_multiplier,
            )

            intent = self.router.evaluate(
                regime, snapshot.candles, decision_id, snapshot.symbol,
                snapshot.timeframe, snapshot.last, snapshot=snapshot,
            )
            strategy_trace = getattr(self.router, "last_trace", {}) or {}

            # Keep production logs operationally useful.  Strategy internals can
            # contain many nested checks; Railway only needs the selected model
            # and the final reason/score.  Full traces remain available in-memory
            # via the engine state when needed for targeted debugging.
            selected_strategy = strategy_trace.get("selected")
            selected_reason = strategy_trace.get("reason")
            compact_trace = {
                "selected": selected_strategy,
                "reason": selected_reason,
            }
            for name in ("breakout", "sweep"):
                branch = strategy_trace.get(name)
                if isinstance(branch, dict):
                    compact_trace[name] = {
                        k: branch.get(k) for k in ("accepted", "reason", "score", "side", "direction")
                        if k in branch
                    }

            self.audit.event(
                "STRATEGY_EVALUATED", decision_id, user_id=user_id, mode=self.execution_mode,
                symbol=snapshot.symbol, regime=regime_meta.get("active"), trace=compact_trace,
            )
            if not intent:
                self.last_decision[key] = now
                # Surface one concise rejection reason instead of the full nested
                # strategy trace.  Prefer the branch that actually ran.
                rejection_reason = "no_valid_setup"
                for name in ("breakout", "sweep"):
                    branch = strategy_trace.get(name)
                    if isinstance(branch, dict) and branch.get("reason") not in (None, "not_run", "regime_breakout_not_allowed", "regime_sweep_not_allowed"):
                        rejection_reason = str(branch.get("reason"))
                        break
                self.audit.event(
                    "SIGNAL_REJECTED", decision_id, user_id=user_id, mode=self.execution_mode,
                    symbol=snapshot.symbol, regime=regime_meta.get("active"),
                    reason=rejection_reason,
                )
                return None

            entry = float(intent.entry_price)
            stop = float(intent.stop_price)
            target = float(intent.target_price)
            risk_distance = abs(entry - stop)
            reward_distance = abs(target - entry)
            execution_rr = reward_distance / max(risk_distance, 1e-12)
            geometry_ok = (
                math.isfinite(execution_rr) and execution_rr > 0 and
                ((intent.direction == Direction.LONG and stop < entry < target) or
                 (intent.direction == Direction.SHORT and target < entry < stop))
            )
            if not geometry_ok:
                self.last_decision[key] = now
                self.audit.event(
                    "SIGNAL_REJECTED", decision_id, user_id=user_id, mode=self.execution_mode,
                    symbol=snapshot.symbol, strategy=getattr(intent.strategy, "value", str(intent.strategy)),
                    reason="invalid_trade_geometry", entry_price=entry, stop_price=stop,
                    target_price=target, execution_rr=execution_rr,
                )
                return None

            structural_rr = (getattr(intent, "metadata", {}) or {}).get("structural_rr_estimate")
            self.audit.event(
                "SIGNAL_ACCEPTED", decision_id, user_id=user_id, mode=self.execution_mode,
                symbol=snapshot.symbol, strategy=getattr(intent.strategy, "value", str(intent.strategy)),
                direction=getattr(intent.direction, "value", str(intent.direction)), quality=round(float(intent.quality), 2),
                entry_price=entry, stop_price=stop, target_price=target,
                execution_rr=round(execution_rr, 4), structural_rr=structural_rr,
                risk_multiplier=intent.risk_multiplier,
            )
            accepted_signal = True
            try:
                risk = self.risk.evaluate(
                    intent,
                    equity,
                    leverage=getattr(self.execution, "leverage", 1),
                )
            except Exception as exc:
                self.last_decision[key] = now
                terminal_event_emitted = True
                self.audit.event(
                    "PIPELINE_ERROR", decision_id, user_id=user_id, mode=self.execution_mode,
                    symbol=snapshot.symbol, stage="risk_evaluate",
                    error=f"{type(exc).__name__}: {exc}",
                )
                return {"accepted": False, "filled": False, "reason": "risk_evaluation_error"}

            # Emit the risk result before diagnostic persistence. This guarantees that
            # the accepted signal always has a visible continuation in Railway.
            self.audit.event(
                "RISK_EVALUATED", decision_id, user_id=user_id, mode=self.execution_mode,
                symbol=snapshot.symbol, approved=bool(risk.approved), reason=str(risk.reason),
                equity=float(equity), quote_notional=float(risk.quantity),
                base_quantity=float(risk.base_quantity), margin_required=float(risk.margin_required),
                leverage=int(risk.leverage),
            )

            # Keep Mongo lean and BSON-safe: store only primitives needed to reconstruct
            # the accepted decision instead of full indicator/regime/metadata payloads.
            self._safe_decision_update(decision_id, {
                "decision_id": decision_id,
                "symbol": str(snapshot.symbol),
                "timeframe": str(snapshot.timeframe),
                "user_id": user_id,
                "mode": str(self.execution_mode),
                "strategy": getattr(intent.strategy, "value", str(intent.strategy)),
                "direction": getattr(intent.direction, "value", str(intent.direction)),
                "quality": float(intent.quality),
                "entry_price": entry,
                "stop_price": stop,
                "target_price": target,
                "execution_rr": float(execution_rr),
                "structural_rr": float(structural_rr) if structural_rr is not None else None,
                "risk_approved": bool(risk.approved),
                "risk_reason": str(risk.reason),
                "quote_notional": float(risk.quantity),
                "base_quantity": float(risk.base_quantity),
                "margin_required": float(risk.margin_required),
                "leverage": int(risk.leverage),
                "status": "RISK_EVALUATED",
            }, user_id=user_id, symbol=snapshot.symbol)

            if not risk.approved:
                self.last_decision[key] = now
                terminal_event_emitted = True
                self.audit.event(
                    "RISK_REJECTED", decision_id, user_id=user_id, mode=self.execution_mode,
                    symbol=snapshot.symbol, reason=str(risk.reason)
                )
                return {"accepted": False, "filled": False, "reason": str(risk.reason)}

            # Execution requires a usable live order book even in DEMO because
            # demo fills are simulated against CoinW bid/ask. Missing depth must
            # reject the execution, never crash the runtime.
            try:
                market_bid = float(snapshot.bid)
                market_ask = float(snapshot.ask)
            except (TypeError, ValueError):
                market_bid = 0.0
                market_ask = 0.0
            if market_bid <= 0 or market_ask <= 0:
                self.last_decision[key] = now
                result = {'accepted': False, 'filled': False, 'reason': 'market_unavailable'}
                self._safe_decision_update(
                    decision_id, {"execution": result, "status": "EXECUTION_REJECTED"},
                    user_id=user_id, symbol=snapshot.symbol,
                )
                terminal_event_emitted = True
                self.audit.event(
                    "EXECUTION_REJECTED", decision_id, user_id=user_id,
                    mode=self.execution_mode, symbol=snapshot.symbol,
                    reason='market_unavailable', orderbook_valid=getattr(snapshot, 'orderbook_valid', False),
                )
                return result

            try:
                result = self.execution.submit(
                    intent, risk.quantity,
                    {
                        "bid": market_bid,
                        "ask": market_ask,
                        "last": snapshot.last,
                        "ts": now_ms,
                    },
                )
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:
                self.last_decision[key] = now
                terminal_event_emitted = True
                self.audit.event(
                    "PIPELINE_ERROR", decision_id, user_id=user_id, mode=self.execution_mode,
                    symbol=snapshot.symbol, stage="execution_submit",
                    error=f"{type(exc).__name__}: {exc}",
                )
                return {"accepted": False, "filled": False, "reason": "execution_error"}

            self._safe_decision_update(
                decision_id, {"execution": result, "status": "EXECUTION_RESULT"},
                user_id=user_id, symbol=snapshot.symbol,
            )
            self.audit.event("EXECUTION_RESULT", decision_id, user_id=user_id, mode=self.execution_mode, symbol=snapshot.symbol, **result)

            # Every accepted signal must finish in one visible terminal event.
            # Execution engines can reject after submit (for example because the
            # live spread is too wide).  Previously those results were only
            # emitted as DEBUG EXECUTION_RESULT, so at LOG_LEVEL=INFO the
            # pipeline appeared to stop after SIGNAL_ACCEPTED.
            if not result.get("filled"):
                reason = str(result.get("reason") or "not_filled")
                self._safe_decision_update(
                    decision_id, {"execution": result, "status": "EXECUTION_REJECTED"},
                    user_id=user_id, symbol=snapshot.symbol,
                )
                terminal_event_emitted = True
                self.audit.event(
                    "EXECUTION_REJECTED", decision_id, user_id=user_id,
                    mode=self.execution_mode, symbol=snapshot.symbol,
                    reason=reason, accepted=bool(result.get("accepted", False)),
                )
                self.last_decision[key] = now
                return result

            if result.get("filled") and not result.get("position"):
                terminal_event_emitted = True
                self.audit.event(
                    "PIPELINE_ERROR", decision_id, user_id=user_id, mode=self.execution_mode,
                    symbol=snapshot.symbol, stage="execution_result",
                    error="filled_result_missing_position",
                )
                self.last_decision[key] = now
                return {**result, "filled": False, "reason": "filled_result_missing_position"}

            if result.get("filled") and result.get("position"):
                raw = result["position"]
                if isinstance(raw, Position):
                    position = raw
                else:
                    direction = raw["direction"]
                    if not isinstance(direction, Direction):
                        direction = Direction(direction)
                    position = Position(
                        position_id=str(raw["position_id"]),
                        decision_id=str(raw["decision_id"]),
                        symbol=str(raw["symbol"]),
                        direction=direction,
                        quantity=float(raw["quantity"]),
                        entry_price=float(raw["entry_price"]),
                        stop_price=float(raw["stop_price"]),
                        target_price=float(raw["target_price"]),
                        status=raw.get("status", "OPEN"),
                        tp1_price=raw.get("tp1_price"),
                        tp2_price=raw.get("tp2_price"),
                        remaining_quantity=raw.get("remaining_quantity"),
                        opened_at=raw.get("opened_at"),
                        entry_fee=float(raw.get("entry_fee", 0)),
                    )
                strategy_name = getattr(intent.strategy, "value", str(intent.strategy))
                position.strategy = strategy_name
                position.quality = round(float(intent.quality), 2)
                position.execution_rr = round(execution_rr, 4)
                if structural_rr is not None:
                    position.structural_rr = structural_rr
                # Persist the filled position before announcing it. The dashboard reads
                # MongoDB, so POSITION_OPENED must mean both execution and platform
                # visibility succeeded. A persistence failure is surfaced explicitly.
                try:
                    position_doc = {**position.__dict__, "user_id": user_id, "mode": self.execution_mode}
                    order_doc = {k: v for k, v in result.items() if k != "position"}
                    order_doc.update({"decision_id": decision_id, "user_id": user_id, "mode": self.execution_mode})
                    self.db.upsert(
                        "positions", {"position_id": position.position_id}, position_doc,
                    )
                    self.db.upsert(
                        "orders",
                        {"order_id": result.get("order_id", position.position_id)},
                        order_doc,
                    )
                except Exception as exc:
                    terminal_event_emitted = True
                    self.audit.event(
                        "PIPELINE_ERROR", decision_id, user_id=user_id, mode=self.execution_mode,
                        symbol=snapshot.symbol, stage="position_persist",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    self.last_decision[key] = now
                    return {"accepted": True, "filled": False, "reason": "position_persist_error"}

                self.position_manager.add(position)
                terminal_event_emitted = True
                self.audit.event(
                    "POSITION_OPENED", decision_id, user_id=user_id, mode=self.execution_mode,
                    symbol=position.symbol, position_id=position.position_id,
                    direction=getattr(position.direction, "value", str(position.direction)),
                    quantity=position.quantity, entry_price=position.entry_price, stop_price=position.stop_price,
                    target_price=position.target_price, strategy=strategy_name, execution_rr=round(execution_rr, 4),
                )
                if self.on_position_opened:
                    try:
                        self.on_position_opened(position)
                    except Exception as exc:
                        # Telegram notification is best effort and must never turn a
                        # successfully persisted/opened trade into a pipeline failure.
                        self.audit.event(
                            "TELEGRAM_NOTIFY_FAILED", decision_id, level="ERROR",
                            user_id=user_id, mode=self.execution_mode, symbol=position.symbol,
                            error=f"{type(exc).__name__}: {exc}",
                        )

            self.last_decision[key] = now
            return result
        finally:
            if accepted_signal and not terminal_event_emitted:
                self.audit.event(
                    "PIPELINE_ERROR", decision_id, user_id=user_id, mode=self.execution_mode,
                    symbol=getattr(snapshot, "symbol", None), stage="terminal_guard",
                    error="accepted_signal_without_terminal_event",
                )
            self.inflight.discard(key)
