import asyncio
from types import SimpleNamespace

import pytest

from app.models.enums import Direction
from app.models.trading import Position
from app.orchestrator import TradingOrchestrator
from app.position.exit_engine import ExitEngine
from app.position.manager import PositionManager
from app.position.protection import calculate_break_even_price, front_run_target


class Audit:
    def __init__(self):
        self.events = []

    def event(self, event, decision_id=None, **data):
        self.events.append((event, decision_id, data))


class Persistence:
    def __init__(self):
        self.saved = []

    def schedule_position_save(self, *, position, user_id, mode, symbol):
        self.saved.append((position.position_id, position.stop_price, mode, symbol))


class LiveProtectionExecution:
    mode = "live"

    def __init__(self):
        self.calls = []

    async def ensure_protection(self, position):
        self.calls.append((position.position_id, position.stop_price, position.target_price))
        return position


def test_structural_target_is_front_run_on_both_sides():
    long_target, ratio = front_run_target(100, 105, Direction.LONG, 0.92)
    short_target, _ = front_run_target(100, 95, Direction.SHORT, 0.92)
    assert ratio == pytest.approx(0.92)
    assert long_target == pytest.approx(104.6)
    assert short_target == pytest.approx(95.4)


def test_break_even_is_fee_aware_and_profit_lock_prevents_full_reversal():
    audit = Audit()
    manager = PositionManager(
        ExitEngine(), audit=audit, evaluate_local_exits=True,
        estimated_exit_fee_rate=0.0006, break_even_buffer_bps=3.0,
    )
    p = Position(
        "p", "d", "BTC", Direction.LONG, 1.0,
        100.0, 99.0, 102.0,
        entry_fee=0.06,
        break_even_activation_ratio=0.55,
        profit_lock_activation_ratio=0.80,
        profit_lock_capture_ratio=0.35,
    )
    manager.add(p, persist=False)

    expected_be = calculate_break_even_price(p, fallback_exit_fee_rate=0.0006, buffer_bps=3.0)
    assert p.break_even_price == pytest.approx(expected_be)
    assert p.break_even_price > p.entry_price

    # 60% of the route to TP: move the stop above fee-aware break-even.
    manager.mark("BTC", 101.20, 1_000)
    assert p.status == "OPEN"
    assert p.management_stage == "BREAK_EVEN"
    assert p.stop_price == pytest.approx(p.break_even_price)
    assert p.stop_price > p.entry_price
    assert not p.protection_update_pending  # DEMO/local exit needs no exchange sync.

    # 85% of the route: lock 35% of the target distance as realised protection.
    manager.mark("BTC", 101.70, 2_000)
    assert p.status == "OPEN"
    assert p.management_stage == "PROFIT_LOCK"
    assert p.stop_price == pytest.approx(100.70)
    assert p.profit_lock_price == pytest.approx(100.70)

    # A full reversal can now stop out only in profit, not at the original SL.
    manager.mark("BTC", 100.65, 3_000)
    assert p.status == "CLOSED"
    assert p.exit_reason == "SL"
    assert p.realized_pnl > 0
    assert any(event == "STOP_MOVED_TO_BREAK_EVEN" for event, _, _ in audit.events)
    assert any(event == "PROFIT_LOCK_ACTIVATED" for event, _, _ in audit.events)


def test_short_profit_lock_is_symmetric():
    manager = PositionManager(ExitEngine(), evaluate_local_exits=True)
    p = Position(
        "p", "d", "BTC", Direction.SHORT, 1.0,
        100.0, 101.0, 98.0,
        entry_fee=0.06,
        break_even_activation_ratio=0.55,
        profit_lock_activation_ratio=0.80,
        profit_lock_capture_ratio=0.35,
    )
    manager.add(p, persist=False)
    manager.mark("BTC", 98.80, 1_000)
    assert p.management_stage == "BREAK_EVEN"
    assert p.stop_price < p.entry_price
    manager.mark("BTC", 98.30, 2_000)
    assert p.management_stage == "PROFIT_LOCK"
    assert p.stop_price == pytest.approx(99.30)
    manager.mark("BTC", 99.35, 3_000)
    assert p.status == "CLOSED"
    assert p.realized_pnl > 0


def test_live_stop_change_is_pushed_to_exchange_protection():
    audit = Audit()
    persistence = Persistence()
    execution = LiveProtectionExecution()
    manager = PositionManager(
        ExitEngine(), audit=audit, evaluate_local_exits=False,
        owner_user_id="u", owner_mode="live",
    )
    p = Position(
        "live-p", "d", "BTC", Direction.LONG, 1.0,
        100.0, 99.0, 102.0, entry_fee=0.06,
    )
    manager.add(p, persist=False)
    orchestrator = TradingOrchestrator(
        regime_engine=object(), router=object(), risk=object(), execution=execution,
        db=None, audit=audit, position_manager=manager, signal_factory=None,
        execution_mode="live", persistence=persistence,
    )
    snap = SimpleNamespace(
        symbol="BTC", timeframe="5m", last=101.20,
        bid=101.20, ask=101.21, quote_received_ms=1_000,
        monitor_only=True,
    )
    asyncio.run(orchestrator.on_snapshot(snap, 100, user_id="u", allow_entries=False))

    assert p.management_stage == "BREAK_EVEN"
    assert p.stop_price > 100
    assert execution.calls
    assert execution.calls[-1][1] == pytest.approx(p.stop_price)
    assert p.protection_update_pending is False
    assert persistence.saved


def test_reconciliation_never_loosens_managed_live_stop():
    manager = PositionManager(ExitEngine(), evaluate_local_exits=False)
    p = Position(
        "p", "d", "BTC", Direction.LONG, 1.0,
        100.0, 100.70, 102.0,
        management_stage="PROFIT_LOCK",
        profit_lock_price=100.70,
        protection_update_pending=True,
    )
    manager.add(p, persist=False)
    row = {
        "id": "p", "instrument": "BTC", "status": "open", "direction": "long",
        "openPrice": "100", "quantityUnit": 0, "quantity": "100",
        "stopLossPrice": "99", "stopProfitPrice": "102",
    }
    manager.reconcile_exchange([row], 5_000, symbol="BTC", persist=False, notify=False)
    assert p.stop_price == pytest.approx(100.70)
    assert p.protection_update_pending is True
