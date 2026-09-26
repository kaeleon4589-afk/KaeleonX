from __future__ import annotations

from types import SimpleNamespace

from app.backtest.replay import StrategyReplay, load_candles_csv
from app.models.enums import Direction, Strategy
from app.models.market import Candle
from app.models.trading import ArmedSetup, TradeIntent


class ReplayRegime:
    last_metadata = {"active": "TREND_CONTINUATION", "scores": {}}

    def evaluate_snapshot(self, snapshot):
        return SimpleNamespace(
            hard_block=False,
            breakout_allowed=True,
            sweep_allowed=False,
            risk_multiplier=1.0,
        )


class ReplayRouter:
    def __init__(self):
        self.last_trace = {}
        self.armed_once = False

    def discover_armed(self, regime, snapshot, symbol, timeframe, regime_metadata=None):
        if self.armed_once:
            self.last_trace = {"armed": {"reason": "no_more_setups"}}
            return None
        self.armed_once = True
        self.last_trace = {"armed": {"accepted": True, "reason": "setup_armed"}}
        return ArmedSetup(
            setup_id="replay-arm", symbol=symbol, strategy=Strategy.BREAKOUT_RETEST,
            direction=Direction.LONG, armed_at_ms=60_000, expires_at_ms=600_000,
            trigger_price=100.0, invalidation_price=99.0,
            stop_price=99.0, target_price=102.0,
            entry_zone_low=100.0, entry_zone_high=100.5,
            quality=90.0, risk_multiplier=1.0, timeframe="5m",
        )

    def trigger_armed(self, setup, snapshot, decision_id):
        intent = TradeIntent(
            decision_id, setup.symbol, setup.strategy, setup.direction,
            100.0, 99.0, 102.0, 90.0, 1.0, "5m",
        )
        return "triggered", intent, {"reason": "test_trigger"}


def _frames():
    # Higher-timeframe history is already closed before the replay window.
    five = [Candle((-260 + i) * 300_000, 100, 100.2, 99.8, 100, 100) for i in range(260)]
    fifteen = [Candle((-200 + i) * 900_000, 100, 100.2, 99.8, 100, 100) for i in range(200)]
    hour = [Candle((-200 + i) * 3_600_000, 100, 100.2, 99.8, 100, 100) for i in range(200)]
    one = [
        Candle(0, 100.0, 100.2, 99.9, 100.0, 100),
        Candle(60_000, 100.0, 100.3, 99.9, 100.1, 100),
        Candle(120_000, 100.1, 102.2, 100.0, 102.0, 100),
    ]
    return {"1m": one, "5m": five, "15m": fifteen, "1h": hour}


def test_replay_runs_armed_trigger_and_trade_outcome():
    report = StrategyReplay(router=ReplayRouter(), regime_engine=ReplayRegime()).run(_frames(), symbol="BTC")
    assert report.summary["trades"] == 1
    assert report.summary["wins"] == 1
    assert report.trades[0].exit_reason == "TP"
    assert report.trades[0].r_multiple == 2.0
    assert report.summary["expectancy_r"] == 2.0


def test_csv_loader_accepts_seconds_and_deduplicates(tmp_path):
    path = tmp_path / "candles.csv"
    path.write_text(
        "timestamp,open,high,low,close,volume\n"
        "100,10,11,9,10.5,20\n"
        "100,10,12,9,11,30\n"
        "160,11,12,10,11.5,25\n",
        encoding="utf-8",
    )
    candles = load_candles_csv(path)
    assert len(candles) == 2
    assert candles[0].timestamp == 100_000
    assert candles[0].high == 12.0
