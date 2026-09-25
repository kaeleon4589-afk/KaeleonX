import asyncio
from types import SimpleNamespace

from app.execution.paper import PaperExecutionEngine
from app.models.enums import Direction, RegimeState, Strategy
from app.models.trading import Position, TradeIntent
from app.orchestrator import TradingOrchestrator
from app.position.exit_engine import ExitEngine
from app.position.manager import PositionManager
from app.risk.manager import RiskManager
from app.storage.database import Database, bson_safe
from app.trading.persistence import TradePersistence


class CapturingAudit:
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
            core_score=85.0,
            breakout_allowed=True,
            sweep_allowed=False,
            hard_block=False,
            risk_multiplier=1.0,
        )


class DynamicRouter:
    def __init__(self):
        self.last_trace = {"selected": "BREAKOUT_RETEST", "reason": "test"}

    def evaluate(self, regime, candles, decision_id, symbol, timeframe, current_price=None, snapshot=None):
        price = float(current_price or 100.0)
        return TradeIntent(
            decision_id=decision_id,
            symbol=symbol,
            strategy=Strategy.BREAKOUT_RETEST,
            direction=Direction.LONG,
            entry_price=price,
            stop_price=price * 0.994,
            target_price=price * 1.0072,
            quality=90.0,
            risk_multiplier=1.0,
            timeframe=timeframe,
            metadata={"execution_rr": 1.2, "structural_stop_pct": 0.006},
        )


def snapshot(symbol="BTC", price=100.0):
    return SimpleNamespace(
        symbol=symbol,
        timeframe="5m",
        candles=[],
        last=price,
        bid=price * 0.9999,
        ask=price * 1.0001,
        orderbook_valid=True,
        data_complete=True,
    )


def build_orchestrator(execution, *, db=None, audit=None, opened=None, mode="demo"):
    db = db or Database()
    audit = audit or CapturingAudit()
    opened = opened if opened is not None else []
    manager = PositionManager(
        ExitEngine(), audit=audit, db=db,
        owner_user_id="u1", owner_mode=mode,
    )
    orchestrator = TradingOrchestrator(
        FixedRegime(), DynamicRouter(), RiskManager(.01, 10), execution,
        db, audit, manager, None,
        execution_mode=mode,
        on_position_opened=opened.append,
        persistence=TradePersistence(db, audit, timeout_seconds=0.25, retries=1),
    )
    return orchestrator, manager, audit, db, opened


def test_bson_boundary_serializes_domain_enums_and_dataclass():
    position = Position(
        "p1", "d1", "BTC", Direction.LONG,
        0.1, 100.0, 99.4, 100.72,
    )
    encoded = bson_safe(position)
    assert encoded["direction"] == "LONG"
    assert encoded["position_id"] == "p1"


def test_reconcile_one_symbol_never_closes_another_symbol():
    audit = CapturingAudit()
    manager = PositionManager(ExitEngine(), audit=audit, owner_user_id="u1", owner_mode="live")
    btc = Position("btc-pos", "btc-dec", "BTC", Direction.LONG, 0.1, 100, 99, 102)
    manager.add(btc, persist=False)
    manager.state_changed = False

    changes = manager.reconcile_exchange([], 123456, symbol="ETH", persist=False, notify=False)

    assert btc.status == "OPEN"
    assert changes["closed"] == []
    assert not any(e[0] == "POSITION_CLOSED" for e in audit.events)


def test_delayed_live_fill_is_imported_without_touching_other_symbols():
    audit = CapturingAudit()
    manager = PositionManager(ExitEngine(), audit=audit, owner_user_id="u1", owner_mode="live")
    btc = Position("btc-pos", "btc-dec", "BTC", Direction.LONG, 0.1, 100, 99, 102)
    manager.add(btc, persist=False)
    manager.state_changed = False

    row = {
        "id": "eth-pos",
        "instrument": "ETH",
        "status": "open",
        "direction": "long",
        "openPrice": "2500",
        "quantityUnit": 2,
        "quantity": "0.02",
        "stopLossPrice": "2485",
        "stopProfitPrice": "2518",
        "thirdOrderId": "eth-dec",
        "updatedDate": 123456,
    }
    changes = manager.reconcile_exchange([row], 123456, symbol="ETH", persist=False, notify=False)

    assert btc.status == "OPEN"
    assert len(changes["opened"]) == 1
    eth = changes["opened"][0]
    assert eth.position_id == "eth-pos"
    assert eth.symbol == "ETH"
    assert eth.status == "OPEN"


class PendingLiveExecution:
    mode = "live"
    leverage = 1

    def __init__(self):
        self.submit_calls = []

    async def submit(self, intent, quantity, market=None):
        self.submit_calls.append(intent.symbol)
        return {
            "accepted": True,
            "filled": False,
            "order_id": "order-1",
            "reason": "order_pending_confirmation",
        }

    async def pending_order_status(self, order_id):
        return {"status": "unknown", "order": None}


def test_pending_live_order_globally_blocks_second_symbol_submission():
    execution = PendingLiveExecution()
    orchestrator, manager, audit, _, _ = build_orchestrator(execution, mode="live")

    first = asyncio.run(orchestrator.on_snapshot(snapshot("BTC"), 100.0, user_id="u1"))
    assert first["accepted"] is True and first["filled"] is False
    assert orchestrator.pending_execution is not None

    second = asyncio.run(orchestrator.on_snapshot(snapshot("ETH", 2500.0), 100.0, user_id="u1"))
    assert second is None
    assert execution.submit_calls == ["BTC"]
    assert any(
        event == "ENTRY_SKIPPED" and data.get("reason") == "execution_pending"
        for event, _, data in audit.events
    )


class FailingDatabase(Database):
    def upsert(self, collection, key, document):
        raise RuntimeError("database_down_for_test")


def test_database_failure_after_fill_cannot_turn_fill_into_rejection():
    db = FailingDatabase()
    audit = CapturingAudit()
    opened = []
    execution = PaperExecutionEngine(audit=audit, max_spread_bps=30, initial_equity=100)
    orchestrator, manager, _, _, _ = build_orchestrator(
        execution, db=db, audit=audit, opened=opened, mode="demo"
    )

    result = asyncio.run(orchestrator.on_snapshot(snapshot("BTC"), 100.0, user_id="u1"))

    assert result["accepted"] is True
    assert result["filled"] is True
    assert len([p for p in manager.positions.values() if p.status == "OPEN"]) == 1
    assert len(opened) == 1
    names = [event for event, _, _ in audit.events]
    assert "POSITION_PERSIST_ERROR" in names
    assert "POSITION_OPENED" in names
    assert "EXECUTION_REJECTED" not in names


def test_paper_fill_execution_result_context_does_not_duplicate_mode_keyword():
    db = Database()
    audit = CapturingAudit()
    execution = PaperExecutionEngine(audit=audit, max_spread_bps=30, initial_equity=100)
    orchestrator, _, _, _, _ = build_orchestrator(execution, db=db, audit=audit, mode="demo")

    result = asyncio.run(orchestrator.on_snapshot(snapshot("BTC"), 100.0, user_id="u1"))

    assert result["filled"] is True
    names = [event for event, _, _ in audit.events]
    assert "EXECUTION_RESULT" in names
    assert "POSITION_OPENED" in names
    assert "PIPELINE_ERROR" not in names


def test_scanner_uses_source_volume_oi_directional_trend_score_and_failsafe():
    import asyncio
    from app.market.scanner import CoinWMarketScanner

    class Client:
        def __init__(self):
            self.fail = False

        async def tickers(self):
            if self.fail:
                raise RuntimeError('temporary_coinw_failure')
            return {'data': [
                # Equal volume: positive directional trend + OI must outrank negative trend.
                {'instrument': 'AAAUSDT', 'last': '10', 'volume': '1000000', 'openInterest': '10000000', 'change24h': '0.03'},
                {'instrument': 'BBBUSDT', 'last': '10', 'volume': '1000000', 'openInterest': '10000000', 'change24h': '-0.03'},
            ]}

    client = Client()
    scanner = CoinWMarketScanner(client, depth=10, cache_seconds=0)
    first = asyncio.run(scanner.ranked())
    assert [row['symbol'] for row in first[:2]] == ['AAA', 'BBB']
    assert first[0]['score'] == 1.0
    assert first[1]['score'] == 0.8
    assert first[0]['change_24h'] == 3.0

    client.fail = True
    fallback = asyncio.run(scanner.ranked())
    assert fallback == first


def test_regime_classifier_matches_source_priority_and_state_confirmation():
    from app.regime.advanced import classify_details, TREND, VOLATILE
    from app.regime.state_machine import advance

    # Strong trend feature vector from the source detector's documented inputs.
    trend = classify_details({
        'context_ok': True, 'adx': 28.0, 'choppiness': 42.0, 'efficiency_ratio': 0.55,
        'wick_instability': 0.20, 'body_quality': 0.70, 'breakout_failure_ratio': 0.05,
        'atr_pct': 0.004, 'btc_shock_ratio': 0.4, 'distance_to_vwap_atr': 1.6,
        'ema_stack_alignment': 1.0, 'trend_bias': 'long', 'recent_move_3': 0.004,
        'btc_recent_move_3': 0.001,
    })
    assert trend['candidate_regime'] == TREND

    # Source priority gives a decisive volatile setup precedence over trend/range.
    volatile = classify_details({
        'context_ok': True, 'adx': 28.0, 'choppiness': 42.0, 'efficiency_ratio': 0.30,
        'wick_instability': 0.70, 'body_quality': 0.30, 'breakout_failure_ratio': 0.35,
        'atr_pct': 0.012, 'btc_shock_ratio': 1.6, 'distance_to_vwap_atr': 1.0,
        'ema_stack_alignment': 0.70, 'trend_bias': 'long', 'recent_move_3': 0.010,
        'btc_recent_move_3': 0.006,
    })
    assert volatile['candidate_regime'] == VOLATILE

    state = advance(TREND, None, confirm_bars=3, cooldown_bars=2, min_active_bars=3)
    state = advance(VOLATILE, state, confirm_bars=3, cooldown_bars=2, min_active_bars=3)
    assert state['active'] == TREND
    state = advance(VOLATILE, state, confirm_bars=3, cooldown_bars=2, min_active_bars=3)
    assert state['active'] == TREND
    state = advance(VOLATILE, state, confirm_bars=3, cooldown_bars=2, min_active_bars=3)
    assert state['active'] == VOLATILE


def test_engine_state_persistence_is_cadence_bounded_across_symbol_rotation():
    import asyncio
    from types import SimpleNamespace
    from app.trading.runtime import UserTradingRuntimeManager, UserRuntime

    class DB:
        def __init__(self):
            self.upserts = 0
        def find_many(self, *args, **kwargs):
            return []
        def upsert(self, *args, **kwargs):
            self.upserts += 1

    class Profiles:
        def __init__(self):
            self.equity_updates = 0
        def update_available_equity(self, *args, **kwargs):
            self.equity_updates += 1

    class Audit:
        def event(self, *args, **kwargs):
            pass

    manager = UserTradingRuntimeManager.__new__(UserTradingRuntimeManager)
    manager.db = DB()
    manager.profiles = Profiles()
    manager.audit = Audit()
    manager.settings = SimpleNamespace(engine_state_persist_seconds=15.0)

    pm = SimpleNamespace(positions={})
    orchestrator = SimpleNamespace(
        regime_engine=SimpleNamespace(last_metadata={'active': 'RANGE', 'candidate': 'RANGE'}),
        router=SimpleNamespace(last_trace={'selected': None}),
    )
    runtime = UserRuntime(
        user_id='u1', fingerprint='fp', mode='demo', trading_enabled=True,
        configured_capital=10.0, execution=object(), position_manager=pm,
        orchestrator=orchestrator, coinw_verified=True,
    )
    first = SimpleNamespace(symbol='BTC', last=100.0, markets_scanned=12, candidates=12)
    second = SimpleNamespace(symbol='ETH', last=50.0, markets_scanned=12, candidates=12)

    asyncio.run(manager._persist_state(runtime, 100.0, 10.0, 'ACTIVO', first))
    asyncio.run(manager._persist_state(runtime, 100.0, 10.0, 'ACTIVO', second))
    assert manager.db.upserts == 1
    assert manager.profiles.equity_updates == 0  # DEMO must not overwrite the CoinW LIVE balance.


def test_source_router_defaults_range_no_trade_and_trend_probe_opt_in(monkeypatch):
    from types import SimpleNamespace
    from app.strategy.router import StrategyRouter

    monkeypatch.delenv('STRATEGY_ROUTER_LIQUIDITY_PROBE_ENABLED', raising=False)
    router = StrategyRouter()
    range_regime = SimpleNamespace(hard_block=False, breakout_allowed=False, sweep_allowed=False)
    assert router.evaluate(range_regime, [], 'd1', 'BTC', '5m') is None
    assert router.last_trace['reason'] == 'router_regime_no_trade'

    class FakeStrategy:
        def __init__(self, result=None):
            self.result = result
            self.last_trace = {'accepted': bool(result), 'reason': 'fake'}
            self.calls = 0
        def evaluate(self, *args, **kwargs):
            self.calls += 1
            return self.result

    trend_regime = SimpleNamespace(hard_block=False, breakout_allowed=True, sweep_allowed=False)
    breakout = FakeStrategy(None)
    sweep = FakeStrategy(SimpleNamespace(quality=99.0, strategy=SimpleNamespace(value='LIQUIDITY_SWEEP')))
    router.breakout = breakout
    router.sweep = sweep
    assert router.evaluate(trend_regime, [], 'd2', 'BTC', '5m', regime_metadata={}) is None
    assert breakout.calls == 1
    assert sweep.calls == 0

    monkeypatch.setenv('STRATEGY_ROUTER_LIQUIDITY_PROBE_ENABLED', 'true')
    meta = {
        'candidate': 'VOLATILE_SWEEP', 'confidence': 0.8,
        'scores': {'VOLATILE_SWEEP': 4},
        'features': {'wick_instability': 0.6, 'breakout_failure_ratio': 0.3, 'btc_shock_ratio': 1.4, 'atr_pct': 0.01},
    }
    selected = router.evaluate(trend_regime, [], 'd3', 'BTC', '5m', regime_metadata=meta)
    assert selected is sweep.result
    assert sweep.calls == 1


def test_executable_rr_below_strategy_minimum_rejected_before_order_in_both_modes():
    """A candle setup can deteriorate at the executable quote."""
    for mode in ('demo', 'live'):
        audit = CapturingAudit()
        execution = (PaperExecutionEngine(audit=audit, initial_equity=100)
                     if mode == 'demo' else PendingLiveExecution())
        orchestrator, manager, _, _, _ = build_orchestrator(execution, audit=audit, mode=mode)

        def bb_signal(regime, candles, decision_id, symbol, timeframe, current_price=None, snapshot=None, **kwargs):
            return TradeIntent(decision_id, symbol, Strategy.BREAKOUT_RETEST, Direction.LONG,
                               .01040, .010372, .010526, 85.0, 1.0, timeframe)

        orchestrator.router.evaluate = bb_signal
        result = asyncio.run(orchestrator.on_snapshot(snapshot('BB', .01045495), 100, user_id='u1'))
        assert result is None
        assert not manager.positions
        assert not getattr(execution, 'submit_calls', [])
        assert orchestrator.last_rejection == 'execution_rr_too_low'
        assert any(e == 'SIGNAL_REJECTED' and d.get('execution_rr', 1) < .95
                   for e, _, d in audit.events)
