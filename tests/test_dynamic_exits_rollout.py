from __future__ import annotations

from types import SimpleNamespace

from app.config.settings import Settings
from app.models.enums import Direction
from app.strategy.entry_engine_v2 import EntryLifecycleV2
from app.strategy.router import StrategyRouter
from app.trading.edge_metrics import edge_estimate


def test_retired_fixed_env_does_not_change_runtime_settings(monkeypatch):
    monkeypatch.setenv("V2_FIXED_EXITS_ENABLED", "true")
    monkeypatch.setenv("V2_FIXED_TP_PERCENT", "99")
    monkeypatch.setenv("V2_FIXED_SL_PERCENT", "99")
    s = Settings()
    assert not hasattr(s, "v2_fixed_exits_enabled")
    assert not hasattr(s, "v2_fixed_tp_percent")
    assert not hasattr(s, "v2_fixed_sl_percent")
    router = StrategyRouter()
    assert not hasattr(router.armed, "fixed_exits_enabled")
    assert not hasattr(EntryLifecycleV2(), "fixed_exits_enabled")


def test_dynamic_edges_vary_with_structural_geometry():
    kwargs = dict(direction=Direction.LONG, taker_fee_rate=0.0006, exit_slippage_bps=2)
    a = edge_estimate(entry=100, stop=99.60, target=100.35, **kwargs)
    b = edge_estimate(entry=100, stop=99.20, target=101.40, **kwargs)
    assert a["gross_tp_bps"] == 35
    assert b["gross_tp_bps"] == 140
    assert a["estimated_breakeven_win_rate"] > b["estimated_breakeven_win_rate"]
    assert a["estimated_round_trip_cost_bps"] == 14


def test_short_and_nonviable_targets():
    x = edge_estimate(entry=100, stop=100.4, target=99.8,
                      direction=Direction.SHORT, taker_fee_rate=0.0006,
                      exit_slippage_bps=2)
    assert x["estimated_net_tp_bps"] == 6
    y = edge_estimate(entry=100, stop=99.5, target=100.10,
                      direction=Direction.LONG, taker_fee_rate=0.0006,
                      exit_slippage_bps=2)
    assert y["estimated_net_tp_bps"] < 0


def test_new_demo_trade_preserves_dynamic_targets_at_fill():
    from app.execution.paper import PaperExecutionEngine
    from app.models.enums import Strategy
    from app.models.trading import TradeIntent

    executor = PaperExecutionEngine(initial_equity=100, leverage=10)
    intent = TradeIntent('d', 'BTC', Strategy.BREAKOUT_RETEST, Direction.LONG,
                         100.0, 99.31, 101.14, 85, 1, '5m',
                         metadata={'engine_version': 'v2', 'exit_profile': 'strategy_dynamic'})
    result = executor.submit(intent, 30, {'bid': 99.99, 'ask': 100.01, 'ts': 1})
    assert result['filled']
    assert result['position'].stop_price == 99.31
    assert result['position'].target_price == 101.14
    assert result['position'].fixed_exit_profile is False


def test_dynamic_demo_tp_is_conservatively_capped_but_sl_preserves_gap():
    from app.position.manager import PositionManager
    from app.position.exit_engine import ExitEngine
    from app.models.trading import Position

    pm = PositionManager(ExitEngine(), dynamic_protection_enabled=False)
    pm.exit_slippage_bps = 2.0
    long_trade = Position('p1', 'd1', 'BTC', Direction.LONG, 1, 100, 99.4, 100.9)
    pm.add(long_trade, persist=False)
    pm.mark('BTC', 101.8, 1000, bid=101.8)
    assert long_trade.status == 'CLOSED'
    assert long_trade.exit_price == 100.9  # Never credit 101.8 for 100.9 TP.
    short_trade = Position('p2', 'd2', 'ETH', Direction.SHORT, 1, 100, 100.6, 99.1)
    pm.add(short_trade, persist=False)
    pm.mark('ETH', 101.2, 2000, ask=101.2)
    assert short_trade.status == 'CLOSED'
    assert short_trade.exit_price > 101.2  # Gap beyond SL is counted.


def test_legacy_fixed_open_position_is_not_rewritten():
    from app.position.manager import PositionManager
    from app.position.exit_engine import ExitEngine
    from app.models.trading import Position

    pm = PositionManager(ExitEngine(), dynamic_protection_enabled=False)
    old_position = Position('old', 'd', 'ETH', Direction.LONG, 1,
                            100, 99.55, 100.45, fixed_exit_profile=True)
    pm.add(old_position, persist=False)
    pm.mark('ETH', 101, 1000, bid=101)
    assert old_position.exit_price == 100.45  # Historical compatibility only.
