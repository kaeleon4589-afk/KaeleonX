"""Regression: the actual CoinW futures order book uses `p` and `m`.

The previous parser silently threw away both sides, causing a valid technical
setup to produce SIGNAL_ACCEPTED immediately followed by EXECUTION_REJECTED
reason=market_unavailable, orderbook_valid=false.
"""
import asyncio
from types import SimpleNamespace

from app.execution.paper import PaperExecutionEngine
from app.market.coordinator import MarketCoordinator
from app.models.enums import Direction, RegimeState, Strategy
from app.models.trading import TradeIntent
from app.orchestrator import TradingOrchestrator
from app.position.exit_engine import ExitEngine
from app.position.manager import PositionManager
from app.risk.manager import RiskManager
from app.storage.database import Database
from app.trading.persistence import TradePersistence


def coinw_depth():
    # CoinW's published REST schema: price `p`, base amount `m`.
    return {
        "code": 0,
        "data": {
            "asks": [{"m": "2.5", "p": "0.14135"}, {"m": "1.0", "p": "0.14132"}],
            "bids": [{"m": "1.0", "p": "0.14129"}, {"m": "2.0", "p": "0.14130"}],
            "n": "marscoin",
            "ts": 1775443378356,
        },
        "msg": "",
    }


def test_coinw_depth_public_documented_p_m_fields_are_read():
    bids, asks = MarketCoordinator._parse_depth(coinw_depth())
    assert bids == [(0.14130, 2.0), (0.14129, 1.0)]
    assert asks == [(0.14132, 1.0), (0.14135, 2.5)]


def test_depth_generic_aliases_and_array_levels_still_supported():
    bids, asks = MarketCoordinator._parse_depth({
        "data": {
            "bids": [{"price": "1", "quantity": "5"}, ["0.9", "3"]],
            "asks": [{"price": "1.1", "qty": "6"}, ["1.2", "4"]],
        }
    })
    assert bids == [(1.0, 5.0), (0.9, 3.0)]
    assert asks == [(1.1, 6.0), (1.2, 4.0)]


def test_depth_ignores_invalid_and_nonfinite_levels():
    bids, asks = MarketCoordinator._parse_depth({"data": {
        "bids": [{"p": "nan", "m": 1}, {"p": -1, "m": 1}, {"p": 1, "m": 0}],
        "asks": [{"p": "inf", "m": 1}],
    }})
    assert bids == [] and asks == []


class FakeCoinW:
    def __init__(self, depth):
        self.book = depth

    async def klines(self, instrument, period, limit):
        return {"code": 0, "data": [
            {"timestamp": (i + 1) * 300_000, "open": 0.14, "high": 0.15,
             "low": 0.13, "close": 0.14131, "volume": 5.0}
            for i in range(320)
        ]}

    async def depth(self, instrument):
        return self.book


def test_snapshot_marks_actual_coinw_orderbook_executable():
    snap = asyncio.run(MarketCoordinator(FakeCoinW(coinw_depth()), "MARSCOIN").snapshot())
    assert snap.symbol == "MARSCOIN"
    assert snap.orderbook_valid is True
    assert snap.data_complete is True
    assert snap.bid == 0.14130 and snap.ask == 0.14132
    assert len(snap.bids) == 2 and len(snap.asks) == 2


def test_snapshot_rejects_crossed_orderbook():
    raw = coinw_depth()
    raw["data"]["asks"] = [{"p": "0.1412", "m": "1"}]
    snap = asyncio.run(MarketCoordinator(FakeCoinW(raw), "MARSCOIN").snapshot())
    assert snap.orderbook_valid is False


class CaptureAudit:
    def __init__(self):
        self.events = []

    def event(self, name, decision_id=None, **details):
        self.events.append((name, decision_id, details))
        return {"event": name, "decision_id": decision_id, **details}


class StaticRegime:
    last_metadata = {"features": {}, "state": {}, "active": "TRENDING"}

    def evaluate_snapshot(self, snapshot):
        return SimpleNamespace(global_state=RegimeState.TRENDING, core_score=85,
                               breakout_allowed=True, sweep_allowed=False,
                               hard_block=False, risk_multiplier=1.0)


class StaticRouter:
    last_trace = {"selected": "BREAKOUT_RETEST", "reason": "valid_setup"}

    def evaluate(self, regime, candles, decision_id, symbol, timeframe,
                 current_price=None, snapshot=None):
        entry = float(current_price)
        return TradeIntent(decision_id, symbol, Strategy.BREAKOUT_RETEST,
                           Direction.LONG, entry, entry * 0.994,
                           entry * 1.0072, 91.06, 1.0, timeframe, (), {})


def make_orchestrator():
    audit = CaptureAudit()
    db = Database()
    opened = []
    engine = PaperExecutionEngine(audit=audit, max_spread_bps=30, initial_equity=100)
    manager = PositionManager(ExitEngine(), audit=audit, db=db,
                              owner_user_id="u1", owner_mode="demo")
    orchestrator = TradingOrchestrator(
        StaticRegime(), StaticRouter(), RiskManager(0.01, 5), engine, db,
        audit, manager, None, execution_mode="demo", on_position_opened=opened.append,
        persistence=TradePersistence(db, audit, timeout_seconds=0.5, retries=1),
    )
    return orchestrator, audit, db, opened


def orchestration_snapshot(bid, ask, orderbook_valid):
    return SimpleNamespace(symbol="MARSCOIN", timeframe="5m", candles=[],
                           last=0.14131, bid=bid, ask=ask,
                           orderbook_valid=orderbook_valid, data_complete=True)


def test_invalid_book_does_not_create_misleading_accepted_signal():
    orch, audit, db, opened = make_orchestrator()
    result = asyncio.run(orch.on_snapshot(orchestration_snapshot(None, None, False),
                                          100, user_id="u1"))
    names = [name for name, _, _ in audit.events]
    assert result["reason"] == "invalid_orderbook"
    assert "MARKET_DATA_SKIPPED" in names
    assert "SIGNAL_ACCEPTED" not in names
    assert "POSITION_OPENED" not in names
    assert opened == []
    assert db.find_many("positions") == []


def test_actual_valid_book_runs_signal_to_persisted_position_and_notification():
    orch, audit, db, opened = make_orchestrator()
    result = asyncio.run(orch.on_snapshot(orchestration_snapshot(
        0.14130, 0.14132, True), 100, user_id="u1"))
    names = [name for name, _, _ in audit.events]
    assert result["accepted"] is True and result["filled"] is True
    assert "SIGNAL_ACCEPTED" in names and "POSITION_OPENED" in names
    assert "EXECUTION_REJECTED" not in names
    assert len(opened) == 1
    position = db.find_one("positions", {"position_id": opened[0].position_id})
    assert position is not None
    assert position["mode"] == "demo" and position["symbol"] == "MARSCOIN"
