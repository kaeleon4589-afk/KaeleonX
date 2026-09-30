from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from app.execution.paper import PaperExecutionEngine
from app.market.coordinator import MarketCoordinator, MultiMarketCoordinator
from test_core_pipeline_refactor import build_orchestrator, snapshot
from test_armed_priority_monitor import PriorityRouter, RegimeMustNotRun, _armed_setup


def quote(symbol="BTC", price=100., age=0):
    return SimpleNamespace(symbol=symbol, bid=price-.01, ask=price+.01,
                           bids=[(price-.01, 1)]*6, asks=[(price+.01, 1)]*6,
                           last=price, orderbook_valid=True,
                           quote_received_ms=int(time.time()*1000)-age)


def priority_setup():
    engine, manager, audit, db, _ = build_orchestrator(PaperExecutionEngine(initial_equity=100), mode="demo")
    engine.router = PriorityRouter()
    engine.regime_engine = RegimeMustNotRun()
    engine.armed_setups["BTC"] = _armed_setup()
    snap = snapshot()
    snap.timeframes = {"1m": []}
    snap.monitor_only = snap.armed_monitor = True
    snap.quote_received_ms = int(time.time()*1000)-23_681
    return engine, snap, manager, audit, db


async def drain(engine):
    await asyncio.gather(*engine.persistence._background)


def test_stale_23_second_priority_snapshot_refreshes_and_fills_through_orchestrator():
    async def run():
        engine, snap, manager, audit, db = priority_setup()
        old_timestamp = snap.quote_received_ms
        calls = []
        async def refresh(symbol):
            calls.append(symbol)
            return quote(symbol)
        engine.execution_quote_provider = refresh
        result = await engine.on_snapshot(snap, 100, user_id="u1", available_equity=100)
        await drain(engine)
        assert result["filled"] is True
        assert manager.positions
        assert len(calls) == 2  # after consumer waits, then after durable exposure checks
        assert snap.quote_received_ms == old_timestamp  # no retimestamp of shared stale snapshot
        assert engine.rejection_funnel.payload()["filled"] == 1
        assert any(event == "EXECUTION_QUOTE_REFRESHED" for event, _, _ in audit.events)
    asyncio.run(run())


def test_same_snapshot_without_refresh_reproduces_observed_market_unavailable():
    async def run():
        engine, snap, _, _, _ = priority_setup()
        result = await engine.on_snapshot(snap, 100, user_id="u1", available_equity=100)
        await drain(engine)
        assert result["reason"] == "market_unavailable"
        assert engine.rejection_funnel.payload()["submitted"] == 0
    asyncio.run(run())


@pytest.mark.parametrize("bad", ["error", "stale", "future", "wrong_symbol", "crossed", "nan"])
def test_failed_pretrigger_refresh_keeps_setup_for_retry(bad):
    async def run():
        engine, snap, manager, _, _ = priority_setup()
        setup = engine.armed_setups["BTC"]
        async def refresh(symbol):
            if bad == "error":
                raise RuntimeError("depth unavailable")
            q = quote(symbol)
            if bad == "stale": q.quote_received_ms -= 23000
            if bad == "future": q.quote_received_ms += 60000
            if bad == "wrong_symbol": q.symbol = "OTHER"
            if bad == "crossed": q.bid = q.ask+1
            if bad == "nan": q.ask = float("nan")
            return q
        engine.execution_quote_provider = refresh
        assert await engine.on_snapshot(snap, 100, user_id="u1") is None
        assert engine.armed_setups["BTC"] is setup
        assert engine.router.trigger_calls == 0
        assert not manager.positions
        async def recovered(symbol): return quote(symbol)
        engine.execution_quote_provider = recovered
        result = await engine.on_snapshot(snap, 100, user_id="u1")
        await drain(engine)
        assert result["filled"] is True
    asyncio.run(run())


def test_fresh_price_is_revalidated_for_chase_after_trigger():
    async def run():
        engine, snap, manager, _, _ = priority_setup()
        calls = 0
        async def refresh(symbol):
            nonlocal calls
            calls += 1
            return quote(symbol, price=100 if calls == 1 else 100.5)
        engine.execution_quote_provider = refresh
        result = await engine.on_snapshot(snap, 100, user_id="u1")
        await drain(engine)
        assert result is None
        assert engine.last_rejection == "entry_chased_after_signal"
        assert not manager.positions
        assert engine.rejection_funnel.payload()["submitted"] == 0
    asyncio.run(run())


def test_refresh_failure_after_trigger_has_terminal_rejection_without_order():
    async def run():
        engine, snap, manager, audit, _ = priority_setup()
        calls = 0
        async def refresh(symbol):
            nonlocal calls
            calls += 1
            if calls == 2: raise RuntimeError("depth unavailable")
            return quote(symbol)
        engine.execution_quote_provider = refresh
        result = await engine.on_snapshot(snap, 100, user_id="u1")
        await drain(engine)
        assert result["reason"] == "execution_quote_refresh_failed"
        assert not manager.positions
        assert any(e == "EXECUTION_REJECTED" and d.get("reason") == result["reason"] for e, _, d in audit.events)
    asyncio.run(run())


def test_slow_claim_cannot_submit_an_expired_quote(monkeypatch):
    async def run():
        engine, snap, manager, _, db = priority_setup()
        clock = [time.time()]
        monkeypatch.setattr("app.orchestrator.time.time", lambda: clock[0])
        async def refresh(symbol): return quote(symbol)
        engine.execution_quote_provider = refresh
        original = db.set_once
        def slow_claim(collection, *args, **kwargs):
            if collection == "signal_claims": clock[0] += 11
            return original(collection, *args, **kwargs)
        monkeypatch.setattr(db, "set_once", slow_claim)
        result = await engine.on_snapshot(snap, 100, user_id="u1")
        await drain(engine)
        assert result["reason"] == "execution_quote_expired_before_submit"
        assert not manager.positions
        assert engine.rejection_funnel.payload()["submitted"] == 0
    asyncio.run(run())


def test_priority_monitor_delivers_ready_symbol_without_waiting_for_slow_symbol(monkeypatch):
    async def run():
        slow_started = asyncio.Event()
        slow_cancelled = asyncio.Event()
        async def fetch(self, symbol):
            if symbol == "SLOW":
                slow_started.set()
                try: await asyncio.Event().wait()
                finally: slow_cancelled.set()
            await slow_started.wait()
            return SimpleNamespace(symbol=symbol)
        monkeypatch.setattr(MarketCoordinator, "armed_snapshot", fetch)
        received = []
        async def callback(snap):
            received.append(snap.symbol)
            raise asyncio.CancelledError()
        market = MultiMarketCoordinator(None, None)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(market.monitor_armed(callback, lambda: ["SLOW", "FAST"]), timeout=1)
        assert received == ["FAST"]
        assert slow_cancelled.is_set()
    asyncio.run(run())


@pytest.mark.parametrize("side", ["long", "short"])
def test_real_strategy_risk_orchestrator_and_paper_fill_after_queue_delay(monkeypatch, side):
    from app.config.settings import Settings
    from app.models.market import Candle
    from app.strategy.router import StrategyRouter
    from test_armed_entry_v6 import _range_sweep_snapshot
    from test_prearm_recovery_v620 import context, reflect

    async def run():
        monkeypatch.setenv("TRADE_PREARM_PROFILE", "recovery")
        engine, manager, audit, _, _ = build_orchestrator(PaperExecutionEngine(initial_equity=100, leverage=10))
        engine.entry_min_stop_atr = Settings().trade_entry_min_stop_atr
        router = StrategyRouter()
        engine.router = router
        engine.regime_engine = RegimeMustNotRun()
        snap = _range_sweep_snapshot()
        if side == "short": snap = reflect(snap)
        regime, meta = context(side)
        setup = router.discover_armed(regime, snap, "TEST", "5m", meta)
        assert setup is not None
        engine.armed_setups["TEST"] = setup
        now = setup.armed_at_ms + 120000
        monkeypatch.setattr("app.orchestrator.time.time", lambda: now/1000)
        trigger, sign = setup.trigger_price, 1 if side == "long" else -1
        micro = [Candle(now-(30-i)*60000, trigger, trigger+.03, trigger-.03, trigger, 100) for i in range(30)]
        opened, close = trigger-sign*.06, trigger+sign*.03
        micro[-1] = Candle(now-60000, opened, max(opened,close)+.005, min(opened,close)-.005, close, 200)
        snap.timeframes["1m"] = micro
        snap.monitor_only = snap.armed_monitor = True
        snap.quote_received_ms = now-23681
        async def refresh(symbol): return quote(symbol, price=trigger+sign*.01)
        engine.execution_quote_provider = refresh
        result = await engine.on_snapshot(snap, 3, user_id="u1", available_equity=100)
        await drain(engine)
        assert result and result["filled"], engine.last_rejection
        assert manager.positions
        assert engine.rejection_funnel.payload()["submitted"] == 1
        assert engine.rejection_funnel.payload()["filled"] == 1
        assert any(event == "POSITION_OPENED" for event, _, _ in audit.events)
    asyncio.run(run())


@pytest.mark.parametrize("event,level", [("EXECUTION_QUOTE_REFRESHED", 20), ("EXECUTION_QUOTE_PENDING", 30)])
def test_quote_diagnostics_are_visible_and_throttled_at_normal_log_level(event, level):
    from app.logging.logger import AuditLogger
    audit = AuditLogger()
    messages = []
    audit.logger = SimpleNamespace(log=lambda numeric, message: messages.append((numeric, message)))
    audit.event(event, "test", user_id="u", symbol="BTC")
    audit.event(event, "test", user_id="u", symbol="BTC")
    assert len(messages) == 1 and messages[0][0] == level
