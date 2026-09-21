from uuid import uuid4
import inspect
import time

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
            self.audit.event(
                "STRATEGY_EVALUATED", decision_id, user_id=user_id, mode=self.execution_mode,
                symbol=snapshot.symbol, regime=regime_meta.get("active"), trace=strategy_trace,
            )
            if not intent:
                self.last_decision[key] = now
                self.audit.event(
                    "SIGNAL_REJECTED", decision_id, user_id=user_id, mode=self.execution_mode,
                    symbol=snapshot.symbol, regime=regime_meta.get("active"),
                    reason="no_valid_setup", strategy_trace=strategy_trace,
                )
                return None

            self.audit.event(
                "SIGNAL_ACCEPTED", decision_id, user_id=user_id, mode=self.execution_mode,
                symbol=snapshot.symbol, strategy=getattr(intent.strategy, "value", str(intent.strategy)),
                direction=getattr(intent.direction, "value", str(intent.direction)), quality=intent.quality,
                entry_price=intent.entry_price, stop_price=intent.stop_price, target_price=intent.target_price,
                risk_multiplier=intent.risk_multiplier, metadata=getattr(intent, "metadata", {}),
            )
            risk = self.risk.evaluate(
                intent,
                equity,
                leverage=getattr(self.execution, "leverage", 1),
            )
            self.db.upsert("decisions", {"decision_id": decision_id}, {
                "decision_id": decision_id,
                "symbol": snapshot.symbol,
                "timeframe": snapshot.timeframe,
                "user_id": user_id,
                "mode": self.execution_mode,
                "regime": getattr(self.regime_engine, "last_metadata", {}) or {},
                "intent": intent.__dict__,
                "risk": risk.__dict__,
                "status": "RISK_EVALUATED",
            })
            self.audit.event(
                "RISK_EVALUATED", decision_id, user_id=user_id, mode=self.execution_mode,
                symbol=snapshot.symbol, approved=risk.approved, reason=risk.reason,
                equity=equity, quote_notional=risk.quantity, base_quantity=risk.base_quantity,
                margin_required=risk.margin_required, leverage=risk.leverage,
            )
            if not risk.approved:
                self.last_decision[key] = now
                self.audit.event(
                    "RISK_REJECTED", decision_id, user_id=user_id, mode=self.execution_mode,
                    symbol=snapshot.symbol, reason=risk.reason
                )
                return None

            result = self.execution.submit(
                intent, risk.quantity,
                {
                    "bid": snapshot.bid,
                    "ask": snapshot.ask,
                    "last": snapshot.last,
                    "ts": now_ms,
                },
            )
            if inspect.isawaitable(result):
                result = await result

            self.db.upsert(
                "decisions", {"decision_id": decision_id},
                {"execution": result, "status": "EXECUTION_RESULT"},
            )
            self.audit.event("EXECUTION_RESULT", decision_id, user_id=user_id, mode=self.execution_mode, symbol=snapshot.symbol, **result)

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
                self.position_manager.add(position)
                self.db.upsert(
                    "positions", {"position_id": position.position_id},
                    {**position.__dict__, "user_id": user_id, "mode": self.execution_mode},
                )
                self.db.upsert(
                    "orders",
                    {"order_id": result.get("order_id", position.position_id)},
                    {**result, "decision_id": decision_id, "user_id": user_id, "mode": self.execution_mode},
                )
                self.audit.event(
                    "POSITION_OPENED", decision_id, user_id=user_id, mode=self.execution_mode,
                    symbol=position.symbol, position_id=position.position_id,
                    direction=getattr(position.direction, "value", str(position.direction)),
                    quantity=position.quantity, entry_price=position.entry_price, stop_price=position.stop_price,
                    target_price=position.target_price, strategy=getattr(intent.strategy, "value", str(intent.strategy)),
                )
                if self.on_position_opened:
                    self.on_position_opened(position)

            self.last_decision[key] = now
            return result
        finally:
            self.inflight.discard(key)
