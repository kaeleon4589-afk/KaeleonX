"""Sweep reversal may wait, but cannot execute solely on a fresh quote."""
from dataclasses import replace
from types import SimpleNamespace
import time
import asyncio

from app.models.enums import Direction, Strategy
from app.models.market import Candle
from app.models.trading import ArmedSetup
from app.strategy.entry_engine_v2 import EntryLifecycleV2
from app.market.chart_service import ChartMarketService


def _setup(now: int, direction: Direction = Direction.LONG) -> ArmedSetup:
    return ArmedSetup(
        setup_id=f"sweep-live-test-{direction.value}", symbol="WLDPROPW",
        strategy=Strategy.LIQUIDITY_SWEEP, direction=direction,
        armed_at_ms=now - 70_000, expires_at_ms=now + 300_000,
        trigger_price=100.0, invalidation_price=98.7 if direction == Direction.LONG else 101.3,
        stop_price=98.9 if direction == Direction.LONG else 101.1,
        target_price=102.5 if direction == Direction.LONG else 97.5,
        entry_zone_low=99.7, entry_zone_high=100.2, quality=87.0, risk_multiplier=1.0,
        timeframe="5m", metadata={"atr_value": 1.0, "minimum_viable_rr": 0.70,
                                    "signal_entry_price": 100.0, "structural_target_price": 102.5 if direction == Direction.LONG else 97.5},
    )


def _snap(now: int, last_candle: Candle, *, imbalance: str = "neutral", ask: float = 100.06):
    bid = ask - 0.01
    bids = [(bid, 50.0)] * 12
    asks = [(ask, 50.0)] * 12
    if imbalance == "sell":
        bids = [(bid, 30.0)] * 12
        asks = [(ask, 70.0)] * 12
    return SimpleNamespace(bid=bid, ask=ask, bids=bids, asks=asks,
                           quote_received_ms=now - 100, timeframes={"1m": [last_candle]},
                           orderbook_valid=True)


def test_long_sweep_waits_through_weak_1m_and_sell_pressure_then_confirms(monkeypatch):
    now = int(time.time() * 1000)
    monkeypatch.setattr('app.strategy.entry_engine_v2.time.time', lambda: now / 1000)
    setup = _setup(now)
    engine = EntryLifecycleV2(fast_confirm_enabled=True)
    weak = Candle(now - 60_000, 100.10, 100.13, 99.62, 99.78, 100)
    status, intent, trace = engine.trigger(setup, _snap(now, weak), "weak")
    assert status == "pending" and intent is None
    assert trace["reason"] == "micro_confirmation_pending"
    bullish = Candle(now - 60_000, 99.80, 100.12, 99.71, 100.05, 100)
    status, intent, trace = engine.trigger(setup, _snap(now, bullish, imbalance="sell"), "sell")
    assert status == "pending" and intent is None
    assert trace["reason"] == "sweep_live_reversal_pending"
    status, intent, trace = engine.trigger(setup, _snap(now, bullish), "reclaimed")
    assert status == "triggered" and intent is not None, trace
    assert intent.metadata["micro_confirmation"]["mode"] == "postarm_closed_1m"


def test_long_sweep_closed_candle_must_still_hold_live_reclaim(monkeypatch):
    now = int(time.time() * 1000)
    monkeypatch.setattr('app.strategy.entry_engine_v2.time.time', lambda: now / 1000)
    setup = _setup(now)
    bullish = Candle(now - 60_000, 99.80, 100.12, 99.71, 100.05, 100)
    status, intent, trace = EntryLifecycleV2().trigger(setup, _snap(now, bullish, ask=99.65), "dip")
    assert status == "pending" and intent is None, trace
    assert trace["reason"] == "sweep_live_reversal_pending"


class _TickerVerifiedMarket:
    def __init__(self):
        self.calls = []
    async def instruments(self):
        return {"code": 0, "data": [{"base": "BTC", "quote": "USDT", "status": "online"}]}
    async def ticker(self, pair):
        self.calls.append(("ticker", pair))
        return {"code": 0, "data": {"last_price": 0.541}} if pair == "WLDPROPW" else {"code": 0, "data": {}}
    async def klines(self, pair, period, size):
        self.calls.append(("klines", pair, period, size))
        if pair not in {"WLDPROPW", "HISTORYONLY"}:
            return {"code": 0, "data": []}
        return {"code": 0, "data": [[1791630000000, .542, .545, .540, .544, 100.0]]}


def test_chart_catalog_miss_can_be_verified_by_coinw_ticker():
    async def run():
        fake = _TickerVerifiedMarket()
        service = ChartMarketService(fake)
        symbol = "WLDPROPWUSDT"
        verified = await service.instrument(symbol)
        assert verified and verified["pair_code"] == "WLDPROPW"
        assert verified["status"] == "verified_ticker"
        candles = await service.candles(symbol, "5m", 80)
        assert len(candles) == 1 and candles[0]["close"] == .544
        assert fake.calls.count(("ticker", "WLDPROPW")) == 1
        assert await service.instrument("FAKEXXXUSDT") is None
    asyncio.run(run())


def test_short_sweep_needs_bearish_confirmed_1m(monkeypatch):
    now = int(time.time() * 1000)
    monkeypatch.setattr('app.strategy.entry_engine_v2.time.time', lambda: now / 1000)
    setup = _setup(now, Direction.SHORT)
    engine = EntryLifecycleV2(fast_confirm_enabled=True)
    bullish = Candle(now - 60_000, 99.95, 100.20, 99.90, 100.11, 100)
    status, intent, trace = engine.trigger(setup, _snap(now, bullish, ask=99.95), 'early')
    assert status == 'pending' and intent is None, trace
    bearish = Candle(now - 60_000, 100.20, 100.25, 99.90, 99.95, 100)
    status, intent, trace = engine.trigger(setup, _snap(now, bearish, ask=99.95), 'reversal')
    assert status == 'triggered' and intent is not None, trace
    assert intent.metadata['micro_confirmation']['mode'] == 'postarm_closed_1m'


def test_chart_failure_warning_remains_independent_of_websocket_status():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] /
              'frontend/src/components/trading/LiveMarketChart.tsx').read_text()
    assert 'historyWarning' in source
    assert 'setHistoryWarning(`No se pudo recuperar el histórico' in source
    assert 'Las velas en vivo pueden seguir apareciendo' in source


def test_history_only_new_listing_uses_real_ohlc_not_synthetic():
    async def run():
        fake = _TickerVerifiedMarket()
        service = ChartMarketService(fake)
        found = await service.instrument("HISTORYONLYUSDT")
        assert found and found["pair_code"] == "HISTORYONLY"
        actual = await service.candles("HISTORYONLYUSDT", "5m", 80)
        assert actual and actual[0]["open"] == .542
        assert await service.instrument("MADEUPUSDT") is None
    asyncio.run(run())
