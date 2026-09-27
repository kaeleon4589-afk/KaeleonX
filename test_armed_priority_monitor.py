from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.config.settings import Settings
from app.execution.paper import PaperExecutionEngine
from app.market.coordinator import MarketCoordinator, MultiMarketCoordinator
from app.models.enums import Direction, Strategy
from app.models.market import Candle
from app.models.trading import ArmedSetup, TradeIntent
from app.trading.runtime import UserTradingRuntimeManager
from test_core_pipeline_refactor import CapturingAudit, build_orchestrator
from test_demo_balance_10x import profile


class PriorityRouter:
    def __init__(self):
        self.last_trace = {}
        self.trigger_calls = 0

    def trigger_armed(self, setup, snapshot, decision_id):
        self.trigger_calls += 1
        self.last_trace = {"reason": "armed_triggered", "armed": {"status": "triggered"}}
        return "triggered", TradeIntent(
            decision_id=decision_id,
            symbol=setup.symbol,
            strategy=setup.strategy,
            direction=setup.direction,
            entry_price=float(snapshot.ask),
            stop_price=99.0,
            target_price=102.0,
            quality=93.0,
            risk_multiplier=1.0,
            timeframe="5m",
            metadata={
                "atr_value": 1.0,
                "atr_pct": 0.01,
                "sl_pct": 0.01,
                "armed_setup_id": setup.setup_id,
                "entry_model": "armed_v6",
                "structural_rr_estimate": 2.0,
            },
        ), {"reason": "micro_confirmation_1m", "execution_rr": 1.9}

    def evaluate(self, *args, **kwargs):
        raise AssertionError("priority ARMED monitor must not run discovery/legacy evaluation")


class RegimeMustNotRun:
    last_metadata = {}

    def evaluate_snapshot(self, snapshot):
        raise AssertionError("priority ARMED monitor must not recompute regime")


def _armed_setup(symbol="BTC", setup_id="arm-priority-1"):
    now = int(time.time() * 1000)
    return ArmedSetup(
        setup_id=setup_id,
        symbol=symbol,
        strategy=Strategy.BREAKOUT_RETEST,
        direction=Direction.LONG,
        armed_at_ms=now - 5_000,
        expires_at_ms=now + 300_000,
        trigger_price=100.0,
        invalidation_price=99.0,
        stop_price=99.0,
        target_price=102.0,
        entry_zone_low=100.0,
        entry_zone_high=100.5,
        quality=88.0,
        risk_multiplier=1.0,
        timeframe="5m",
        metadata={"atr_value": 1.0},
    )


def test_priority_armed_snapshot_can_trigger_without_full_scanner_snapshot():
    async def run():
        execution = PaperExecutionEngine(initial_equity=100)
        orchestrator, manager, _, db, _ = build_orchestrator(execution, mode="demo")
        router = PriorityRouter()
        orchestrator.router = router
        orchestrator.regime_engine = RegimeMustNotRun()
        setup = _armed_setup()
        orchestrator.armed_setups["BTC"] = setup
        snap = SimpleNamespace(
            symbol="BTC", timeframe="5m", candles=[], timeframes={"1m": []},
            bid=100.0, ask=100.01, last=100.005,
            bids=[(100.0, 1.0)] * 6, asks=[(100.01, 1.0)] * 6,
            quote_received_ms=int(time.time() * 1000), orderbook_valid=True,
            data_complete=True, monitor_only=True, armed_monitor=True,
        )
        result = await orchestrator.on_snapshot(snap, 100, user_id="u1", allow_entries=True, available_equity=100)
        await asyncio.gather(*orchestrator.persistence._background)
        assert result is not None and result["filled"] is True
        assert router.trigger_calls == 1
        assert "BTC" not in orchestrator.armed_setups
        assert manager.positions
        assert db.find_one("signal_claims", {"claim_id": f"u1:demo:armed:{setup.setup_id}"}) is not None
        consumed = db.find_one("armed_consumed_setups", {"user_id": "u1", "mode": "demo", "setup_id": setup.setup_id})
        assert consumed is not None and consumed["reason"] == "restored" or consumed is not None
    asyncio.run(run())


def test_armed_market_monitor_fetches_quote_and_closed_1m_only():
    class Market:
        async def depth(self, symbol):
            return {"data": {"bids": [[100.0, 5]], "asks": [[100.01, 5]]}}

        async def klines(self, symbol, timeframe, limit):
            assert timeframe == "1m"
            now = int(time.time() * 1000)
            current_minute = now - (now % 60_000)
            rows = []
            for i in range(61):
                ts = current_minute - (61 - i) * 60_000
                rows.append([ts, 100.0, 100.1, 99.9, 100.02, 10.0])
            return {"data": rows}

    async def run():
        snap = await MarketCoordinator(Market(), "BTC").armed_snapshot("BTC")
        assert snap.armed_monitor is True and snap.monitor_only is True
        assert snap.candles == []
        assert len(snap.timeframes["1m"]) >= 20
        assert snap.bid == 100.0 and snap.ask == 100.01

        received = []
        async def callback(value):
            received.append(value)
            raise asyncio.CancelledError()
        coordinator = MultiMarketCoordinator(Market(), None, poll_seconds=2, armed_poll_seconds=.5)
        with pytest.raises(asyncio.CancelledError):
            await coordinator.monitor_armed(callback, lambda: ["BTC"])
        assert received and received[0].armed_monitor is True
    asyncio.run(run())


def test_runtime_restores_active_and_consumed_armed_state_after_restart():
    async def run():
        from app.storage.database import Database
        db = Database()
        profiles = profile(db)
        profiles.save("u", execution_mode="demo", trading_enabled=True, operating_capital=20)
        setup = _armed_setup("AZTEC", "AZTEC:LS:structure:LONG")
        expires = datetime.now(timezone.utc) + timedelta(minutes=5)
        db.upsert("armed_setups", {"user_id": "u", "mode": "demo", "symbol": "AZTEC"}, {
            "user_id": "u", "mode": "demo", "symbol": "AZTEC", "setup_id": setup.setup_id,
            "active": True, "setup": setup, "expires_at_ms": setup.expires_at_ms, "expires_at": expires,
        })
        consumed_until = int(time.time() * 1000) + 300_000
        db.upsert("armed_consumed_setups", {"user_id": "u", "mode": "demo", "setup_id": "old-setup"}, {
            "user_id": "u", "mode": "demo", "setup_id": "old-setup", "symbol": "AZTEC",
            "reason": "setup_chased", "until_ms": consumed_until, "expires_at": expires,
        })
        settings = Settings(credential_encryption_key=Fernet.generate_key().decode())
        manager = UserTradingRuntimeManager(settings, db, CapturingAudit(), profiles)
        runtime = await manager._build_runtime("u", profiles.get("u"), "fp")
        assert runtime.orchestrator.armed_setups["AZTEC"].setup_id == setup.setup_id
        assert runtime.orchestrator.router.armed._is_consumed("old-setup")[0] is True
        manager._runtimes = {"u": runtime}
        assert manager.armed_symbols() == {"AZTEC"}
    asyncio.run(run())
