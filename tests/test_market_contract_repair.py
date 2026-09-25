import asyncio
import time
from types import SimpleNamespace

import pytest

from app.market.coordinator import MarketCoordinator
from app.models.market import Candle
from app.regime.regime_engine import RegimeEngine
from app.execution.demo import DemoExecutionEngine
from app.models.enums import Direction, Strategy
from app.models.trading import TradeIntent


def test_coinw_pm_depth_sorted_validated():
    raw = {'data': {'bids': [{'p': '99', 'm': '1'}, {'p': '100', 'm': '.2'}],
                    'asks': [{'p': '102', 'm': '1'}, {'p': '101', 'm': '.1'}]}}
    bids, asks = MarketCoordinator._parse_depth(raw)
    assert bids[0] == (100, .2)
    assert asks[0] == (101, .1)


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), 0, -1, None])
def test_bad_quotes_never_create_demo_position(bad):
    ex = DemoExecutionEngine(initial_equity=100)
    intent = TradeIntent('d', 'BTC', Strategy.BREAKOUT_RETEST, Direction.LONG, 100, 99, 102, 90, 1, '5m')
    assert not ex.submit(intent, 50, {'bid': bad, 'ask': bad})['filled']
    assert ex.equity == 100 and not ex.positions


def test_crossed_book_rejected():
    assert MarketCoordinator._parse_depth({'data': {'bids': [[102, 1]], 'asks': [[101, 1]]}}) == ([], [])


class Market:
    def __init__(self, stale=False, gap=False):
        self.stale, self.gap = stale, gap
    async def klines(self, symbol, period, size):
        span = {'5m':300_000, '15m':900_000, '1h':3_600_000}[period]
        bucket = int(time.time()*1000) // span * span
        if self.stale:
            bucket -= span * 5
        rows = [[bucket-i*span,100,101,99,100,10] for i in range(size)]
        if self.gap:
            rows.pop(4)
        return {'data': rows}
    async def depth(self, symbol):
        return {'data': {'bids':[{'p':99.99,'m':10}], 'asks':[{'p':100.01,'m':10}]}}


def test_snapshot_uses_closed_candles_and_fresh_executable_quotes():
    snap = asyncio.run(MarketCoordinator(Market(), 'BTC').snapshot())
    assert snap.bid == 99.99 and snap.ask == 100.01
    assert len(snap.candles) == 320
    assert snap.candles[-1].timestamp + 300_000 <= time.time()*1000
    assert snap.orderbook_valid and snap.data_complete


@pytest.mark.parametrize('args,reason', [({'stale':True},'stale_candles'), ({'gap':True},'candle_gap')])
def test_incomplete_or_stale_market_blocks_entry(args, reason):
    with pytest.raises(RuntimeError, match=reason):
        asyncio.run(MarketCoordinator(Market(**args), 'BTC').snapshot())


def test_regime_confirms_distinct_bars_only():
    cs = [Candle(i*300000,100+i*.1,100.2+i*.1,99.8+i*.1,100.1+i*.1,10) for i in range(320)]
    snap=SimpleNamespace(symbol='BTC',candles=cs,btc_candles=None)
    engine=RegimeEngine()
    for _ in range(4): engine.evaluate_snapshot(snap)
    assert engine.last_metadata['state']['bars']==1
    snap.candles=[*cs,Candle(320*300000,132,132.2,131.8,132.1,10)]
    engine.evaluate_snapshot(snap)
    assert engine.last_metadata['state']['bars']==2
