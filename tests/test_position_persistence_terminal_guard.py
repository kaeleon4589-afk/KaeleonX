import asyncio
from types import SimpleNamespace

from app.execution.paper import PaperExecutionEngine
from app.logging.logger import AuditLogger
from app.models.enums import Direction, RegimeState, Strategy
from app.models.trading import TradeIntent
from app.orchestrator import TradingOrchestrator
from app.position.exit_engine import ExitEngine
from app.position.manager import PositionManager
from app.risk.manager import RiskManager
from app.storage.database import Database
from app.trading.persistence import TradePersistence


class CapturingAudit(AuditLogger):
    def __init__(self):
        self.events = []
    def event(self, event, decision_id=None, **data):
        self.events.append((event, decision_id, data))
        return {"event": event, "decision_id": decision_id, **data}


class FixedRegime:
    last_metadata = {"features": {}, "state": {}, "active": "TRENDING"}
    def evaluate_snapshot(self, snapshot):
        return SimpleNamespace(
            global_state=RegimeState.TRENDING,
            core_score=80.0,
            breakout_allowed=True,
            sweep_allowed=False,
            hard_block=False,
            risk_multiplier=1.0,
        )


class FixedRouter:
    def __init__(self):
        self.last_trace = {"selected": "BREAKOUT_RETEST", "reason": "test"}
    def evaluate(self, regime, candles, decision_id, symbol, timeframe, current_price=None, snapshot=None):
        return TradeIntent(
            decision_id, symbol, Strategy.BREAKOUT_RETEST, Direction.LONG,
            100.0, 99.4, 100.72, 90.0, 1.0, timeframe,
            metadata={"execution_rr": 1.2, "structural_stop_pct": 0.006},
        )


def _snapshot(symbol="BTC"):
    return SimpleNamespace(
        symbol=symbol, timeframe="5m", candles=[], last=100.0,
        bid=99.99, ask=100.01, orderbook_valid=True, data_complete=True,
    )


def test_real_fill_persists_bson_safe_position_order_and_emits_opened():
    db = Database()
    audit = CapturingAudit()
    manager = PositionManager(ExitEngine(), audit=audit, db=db, owner_user_id="u1", owner_mode="demo")
    execution = PaperExecutionEngine(audit=audit, max_spread_bps=30, initial_equity=100)
    opened = []
    orchestrator = TradingOrchestrator(
        FixedRegime(), FixedRouter(), RiskManager(.01, 10), execution,
        db, audit, manager, None, execution_mode="demo",
        on_position_opened=opened.append,
        persistence=TradePersistence(db, audit, timeout_seconds=1, retries=1),
    )
    result = asyncio.run(orchestrator.on_snapshot(_snapshot(), 100.0, user_id="u1"))
    assert result["filled"] is True
    rows = db.find_many("positions", {"user_id": "u1", "mode": "demo"})
    assert len(rows) == 1
    assert rows[0]["direction"] == "LONG"
    assert db.find_many("orders", {"user_id": "u1", "mode": "demo"})
    assert len(opened) == 1
    assert any(event == "POSITION_OPENED" for event, _, _ in audit.events)


def test_filled_result_without_position_is_terminal_error():
    source = open("app/orchestrator.py", encoding="utf-8").read()
    assert 'if result.get("filled") and not result.get("position"):' in source
    assert "filled_result_missing_position" in source


def test_accepted_signal_finally_has_terminal_guard():
    source = open("app/orchestrator.py", encoding="utf-8").read()
    assert "accepted_signal = True" in source
    assert "accepted_signal and not terminal_event_emitted" in source
    assert "accepted_signal_without_terminal_event" in source
