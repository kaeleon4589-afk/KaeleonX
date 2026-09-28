from __future__ import annotations

import asyncio
import time

from app.execution.paper import PaperExecutionEngine
from app.logging import logger as logging_module
from app.models.enums import Direction, Strategy
from app.models.trading import ArmedSetup, SetupWatch, TradeIntent
from test_core_pipeline_refactor import build_orchestrator, snapshot as base_snapshot


class LifecycleRouter:
    def __init__(self):
        self.last_trace = {}

    def discover_armed(self, *args, **kwargs):
        self.last_trace = {"reason": "no_armable_setup", "armed": {"accepted": False, "reason": "not_complete"}}
        return None

    def discover_watch(self, regime, snapshot, symbol, timeframe, regime_metadata=None):
        now = int(time.time() * 1000)
        watch_id = f"{symbol}:BRW:TEST:LONG"
        watch = SetupWatch(
            watch_id=watch_id,
            symbol=symbol,
            strategy=Strategy.BREAKOUT_RETEST,
            direction=Direction.LONG,
            created_at_ms=now,
            expires_at_ms=now + 600_000,
            timeframe=timeframe,
            reasons=("breakout_detected", "waiting_retest"),
            metadata={
                "lifecycle_id": watch_id,
                "lifecycle_origin": "breakout_precursor",
                "risk_multiplier": 1.0,
                "active_regime": "TREND_CONTINUATION",
            },
        )
        self.last_trace = {
            "reason": "setup_watching",
            "watch": {"accepted": True, "reason": "breakout_detected_waiting_retest"},
        }
        return watch

    def advance_watch(self, watch, snapshot):
        now = int(time.time() * 1000)
        setup = ArmedSetup(
            setup_id=f"{watch.symbol}:BR:TEST:LONG",
            symbol=watch.symbol,
            strategy=Strategy.BREAKOUT_RETEST,
            direction=Direction.LONG,
            armed_at_ms=now,
            expires_at_ms=now + 600_000,
            trigger_price=100.0,
            invalidation_price=99.0,
            stop_price=99.0,
            target_price=101.5,
            entry_zone_low=99.95,
            entry_zone_high=100.25,
            quality=85.0,
            risk_multiplier=1.0,
            timeframe="5m",
            reasons=("retest_completed",),
            metadata={
                "lifecycle_id": watch.metadata["lifecycle_id"],
                "origin_watch_id": watch.watch_id,
                "atr_value": 1.0,
                "structural_rr_estimate": 1.5,
            },
        )
        self.last_trace = {"reason": "watch_armed", "watch": {"status": "armed", "reason": "retest_completed"}}
        return "armed", setup, {"reason": "retest_completed", "lifecycle_id": watch.metadata["lifecycle_id"]}

    def trigger_armed(self, setup, snapshot, decision_id):
        intent = TradeIntent(
            decision_id=decision_id,
            symbol=setup.symbol,
            strategy=setup.strategy,
            direction=setup.direction,
            entry_price=100.0,
            stop_price=99.0,
            target_price=101.5,
            quality=90.0,
            risk_multiplier=1.0,
            timeframe="5m",
            reasons=("micro_confirmation_1m",),
            metadata={
                **setup.metadata,
                "armed_setup_id": setup.setup_id,
                "atr_value": 1.0,
                "structural_rr_estimate": 1.5,
            },
        )
        self.last_trace = {"reason": "setup_triggered", "armed": {"status": "triggered", "reason": "micro_confirmation_1m"}}
        return "triggered", intent, {"reason": "micro_confirmation_1m", "price": 100.0}

    def evaluate(self, *args, **kwargs):
        raise AssertionError("legacy path must not execute")


def test_lifecycle_events_are_visible_at_info():
    expected = {
        "SETUP_WATCHING",
        "SETUP_WATCH_PROGRESS",
        "SETUP_WATCH_CANCELLED",
        "SETUP_ARMED",
        "SETUP_ARMED_PROGRESS",
        "SETUP_TRIGGERED",
        "SIGNAL_ACCEPTED",
        "RISK_APPROVED",
        "ORDER_SUBMITTED",
        "POSITION_OPENED",
    }
    assert expected.issubset(logging_module._ALWAYS_INFO)


def test_full_setup_lifecycle_keeps_one_id_until_position_opened():
    execution = PaperExecutionEngine(initial_equity=100, leverage=10)
    orchestrator, manager, audit, db, _ = build_orchestrator(execution, mode="demo")
    orchestrator.router = LifecycleRouter()

    async def run_flow():
        scanner = base_snapshot("BTC", 100.0)
        scanner.timeframes = {"5m": [object()]}
        await orchestrator.on_snapshot(scanner, 100, user_id="u1")

        watching = base_snapshot("BTC", 100.0)
        watching.timeframes = {"5m": [object()]}
        watching.monitor_only = True
        watching.watch_monitor = True
        watching.armed_monitor = False
        await orchestrator.on_snapshot(watching, 100, user_id="u1")

        armed = base_snapshot("BTC", 100.0)
        armed.timeframes = {"1m": [object()]}
        armed.monitor_only = True
        armed.watch_monitor = False
        armed.armed_monitor = True
        result = await orchestrator.on_snapshot(armed, 100, user_id="u1")
        await asyncio.sleep(0.05)
        return result

    result = asyncio.run(run_flow())
    assert result and result["filled"] is True
    assert len(manager.positions) == 1

    milestones = [
        "SETUP_WATCHING",
        "SETUP_ARMED",
        "SETUP_TRIGGERED",
        "SIGNAL_ACCEPTED",
        "RISK_APPROVED",
        "ORDER_SUBMITTED",
        "POSITION_OPENED",
    ]
    events = [item for item in audit.events if item[0] in milestones]
    names = [item[0] for item in events]
    assert names == milestones

    lifecycle_ids = {item[2].get("lifecycle_id") for item in events}
    assert lifecycle_ids == {"BTC:BRW:TEST:LONG"}
    opened = next(item for item in events if item[0] == "POSITION_OPENED")
    assert opened[2]["watch_id"] == "BTC:BRW:TEST:LONG"
    assert opened[2]["setup_id"] == "BTC:BR:TEST:LONG"
    assert opened[2]["lifecycle_complete"] is True

    position = next(iter(manager.positions.values()))
    assert position.lifecycle_id == "BTC:BRW:TEST:LONG"
    assert position.watch_id == "BTC:BRW:TEST:LONG"
    assert position.setup_id == "BTC:BR:TEST:LONG"

    lifecycle_rows = [doc for collection, doc in db.memory if collection == "setup_lifecycles"]
    assert lifecycle_rows
    row = lifecycle_rows[-1]
    assert row["lifecycle_id"] == "BTC:BRW:TEST:LONG"
    assert row["current_stage"] == "POSITION_OPENED"
    assert row["position_id"] == position.position_id
    assert row["terminal"] is True

    ledger = [doc for collection, doc in db.memory if collection == "setup_lifecycle_events"]
    ordered_stages = [doc["current_stage"] for doc in sorted(ledger, key=lambda item: item["stage_order"])]
    assert ordered_stages == [
        "WATCHING", "ARMED", "TRIGGERED", "SIGNAL_ACCEPTED",
        "RISK_APPROVED", "ORDER_SUBMITTED", "POSITION_OPENED",
    ]
    assert {doc["lifecycle_id"] for doc in ledger} == {"BTC:BRW:TEST:LONG"}
