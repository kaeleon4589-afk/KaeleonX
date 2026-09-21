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
                    "POSITION_SYNC_ERROR",
                    "system",
                    symbol=snapshot.symbol,
                    error=str(exc),
                )
        if snapshot.last:
            price = snapshot.last if isinstance(snapshot.last, (int, float)) else snapshot.last.close
            self.position_manager.mark(snapshot.symbol, price, now_ms)

        if self._has_open_position(snapshot.symbol, user_id=user_id):
            return None
        if not allow_entries:
            return None

        key = (snapshot.symbol, snapshot.timeframe)
        now = time.time()
        if now - self.last_decision.get(key, 0) < self.cooldown_seconds:
            return None
        if key in self.inflight:
            return None
        self.inflight.add(key)

        decision_id = uuid4().hex
        try:
            signals = self.signal_factory.build(snapshot)
            regime = self.regime_engine.evaluate(*signals)
            self.db.upsert("decisions", {"decision_id": decision_id}, {
                "decision_id": decision_id,
                "symbol": snapshot.symbol,
                "timeframe": snapshot.timeframe,
                "user_id": user_id,
                "regime": regime.__dict__,
                "status": "REGIME_EVALUATED",
            })
            self.audit.event(
                "REGIME_EVALUATED", decision_id, symbol=snapshot.symbol,
                state=regime.global_state.value, score=regime.core_score,
            )

            intent = self.router.evaluate(
                regime, snapshot.candles, decision_id, snapshot.symbol,
                snapshot.timeframe, snapshot.last,
            )
            if not intent:
                self.last_decision[key] = now
                self.db.upsert(
                    "decisions", {"decision_id": decision_id},
                    {"status": "NO_TRADE", "reason": "no_valid_setup"},
                )
                self.audit.event(
                    "NO_TRADE", decision_id, symbol=snapshot.symbol,
                    reason="no_valid_setup",
                )
                return None

            risk = self.risk.evaluate(
                intent,
                equity,
                leverage=getattr(self.execution, "leverage", 1),
            )
            self.db.upsert("decisions", {"decision_id": decision_id}, {
                "intent": intent.__dict__,
                "risk": risk.__dict__,
                "user_id": user_id,
                "status": "RISK_EVALUATED",
            })
            if not risk.approved:
                self.last_decision[key] = now
                self.audit.event(
                    "RISK_REJECTED", decision_id, reason=risk.reason
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
            self.audit.event("EXECUTION_RESULT", decision_id, **result)

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
                if self.on_position_opened:
                    self.on_position_opened(position)

            self.last_decision[key] = now
            return result
        finally:
            self.inflight.discard(key)
