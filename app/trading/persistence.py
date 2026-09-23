from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any


class TradePersistence:
    """Bounded, async-safe persistence for the critical trading path.

    PyMongo is synchronous. Running writes in the event-loop thread can stall all
    market processing when MongoDB is slow. This adapter moves writes to worker
    threads, applies a hard timeout and keeps diagnostic decision writes outside
    the order-submission critical path.
    """

    def __init__(self, db, audit=None, *, timeout_seconds: float = 4.0, retries: int = 2):
        self.db = db
        self.audit = audit
        self.timeout_seconds = max(0.25, float(timeout_seconds))
        self.retries = max(1, int(retries))
        self._background: set[asyncio.Task] = set()

    @staticmethod
    def compact(value: Any):
        if isinstance(value, Enum):
            return value.value
        if is_dataclass(value) and not isinstance(value, type):
            return TradePersistence.compact(asdict(value))
        if isinstance(value, dict):
            return {str(k): TradePersistence.compact(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [TradePersistence.compact(v) for v in value]
        return value

    async def _upsert(self, collection: str, key: dict, document: dict) -> tuple[bool, str | None]:
        last_error = None
        for attempt in range(1, self.retries + 1):
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        self.db.upsert,
                        collection,
                        self.compact(key),
                        self.compact(document),
                    ),
                    timeout=self.timeout_seconds,
                )
                return True, None
            except asyncio.TimeoutError:
                last_error = f"timeout_after_{self.timeout_seconds:g}s"
            except Exception as exc:  # DB errors are isolated from execution.
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < self.retries:
                await asyncio.sleep(0.10 * attempt)
        return False, last_error or "persistence_failed"

    async def save_fill(self, *, decision_id: str, user_id: str | None, mode: str,
                        position, execution_result: dict) -> tuple[bool, str | None]:
        position_doc = self.compact({**position.__dict__, "user_id": user_id, "mode": mode})
        order_doc = self.compact({
            k: v for k, v in execution_result.items()
            if k not in {"position", "exchange_response", "order_state"}
        })
        order_doc.update({"decision_id": decision_id, "user_id": user_id, "mode": mode})
        order_id = str(execution_result.get("order_id") or position.position_id)

        ok, err = await self._upsert(
            "positions", {"position_id": position.position_id}, position_doc,
        )
        if not ok:
            return False, f"positions:{err}"
        ok, err = await self._upsert(
            "orders", {"order_id": order_id}, order_doc,
        )
        if not ok:
            # The position is already durable, so this is non-fatal for dashboard
            # visibility. Return success but preserve the secondary error.
            return True, f"orders:{err}"
        return True, None


    async def save_position(self, *, position, user_id: str | None, mode: str) -> tuple[bool, str | None]:
        document = self.compact({**position.__dict__, "user_id": user_id, "mode": mode})
        return await self._upsert(
            "positions", {"position_id": position.position_id}, document,
        )

    def schedule_position_save(self, *, position, user_id: str | None, mode: str,
                               symbol: str | None = None) -> None:
        async def runner():
            last_error = None
            for delay in (0.0, 1.0, 3.0, 10.0):
                if delay:
                    await asyncio.sleep(delay)
                ok, last_error = await self.save_position(
                    position=position, user_id=user_id, mode=mode,
                )
                if ok:
                    return
            if self.audit:
                self.audit.event(
                    "POSITION_PERSIST_ERROR", getattr(position, "decision_id", None),
                    level="ERROR", user_id=user_id, mode=mode, symbol=symbol,
                    position_id=getattr(position, "position_id", None),
                    error=last_error or "retry_exhausted",
                )

        try:
            task = asyncio.create_task(runner())
        except RuntimeError:
            return
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def save_order_state(self, *, decision_id: str, user_id: str | None,
                               mode: str, result: dict) -> tuple[bool, str | None]:
        order_id = str(result.get("order_id") or decision_id)
        doc = self.compact({
            k: v for k, v in result.items()
            if k not in {"position", "exchange_response", "order_state"}
        })
        doc.update({"decision_id": decision_id, "user_id": user_id, "mode": mode})
        return await self._upsert("orders", {"order_id": order_id}, doc)

    async def save_decision(self, *, decision_id: str, user_id: str | None,
                            mode: str, document: dict) -> tuple[bool, str | None]:
        doc = self.compact({
            "decision_id": decision_id,
            "user_id": user_id,
            "mode": mode,
            **document,
        })
        return await self._upsert("decisions", {"decision_id": decision_id}, doc)

    def schedule_decision(self, *, decision_id: str, user_id: str | None,
                          mode: str, symbol: str | None, document: dict) -> None:
        """Best-effort diagnostic write after the terminal trading decision.

        This method never delays order placement. Failures are visible but do not
        change the result of an already-completed trade pipeline.
        """
        async def runner():
            ok, err = await self.save_decision(
                decision_id=decision_id, user_id=user_id, mode=mode, document=document,
            )
            if not ok and self.audit:
                self.audit.event(
                    "DECISION_PERSIST_ERROR", decision_id, level="ERROR",
                    user_id=user_id, mode=mode, symbol=symbol, error=err,
                )

        try:
            task = asyncio.create_task(runner())
        except RuntimeError:
            return
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def schedule_fill_retry(self, *, decision_id: str, user_id: str | None, mode: str,
                            symbol: str | None, position, execution_result: dict) -> None:
        async def runner():
            # Give a transient Mongo outage a short chance to recover without
            # holding the market loop. PositionManager memory blocks duplicates.
            for delay in (1.0, 3.0, 10.0):
                await asyncio.sleep(delay)
                ok, err = await self.save_fill(
                    decision_id=decision_id,
                    user_id=user_id,
                    mode=mode,
                    position=position,
                    execution_result=execution_result,
                )
                if ok:
                    if self.audit:
                        self.audit.event(
                            "POSITION_PERSIST_RECOVERED", decision_id,
                            user_id=user_id, mode=mode, symbol=symbol,
                            position_id=getattr(position, "position_id", None),
                        )
                    return
            if self.audit:
                self.audit.event(
                    "POSITION_PERSIST_ERROR", decision_id, level="ERROR",
                    user_id=user_id, mode=mode, symbol=symbol,
                    position_id=getattr(position, "position_id", None),
                    error=err or "retry_exhausted",
                )

        try:
            task = asyncio.create_task(runner())
        except RuntimeError:
            return
        self._background.add(task)
        task.add_done_callback(self._background.discard)
