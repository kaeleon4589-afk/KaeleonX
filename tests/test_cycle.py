import asyncio
from app.market.signal_factory import SignalFactory
from app.models.market import Candle
from app.models.enums import Direction


def candles(n=50, start=100):
    out=[]
    for i in range(n):
        o=start+i*.5; c=o+.4; out.append(Candle(i*60000,o,c+.2,o-.2,c,10))
    return out

def test_signal_factory_produces_non_blocked_signals():
    class S:
        candles=candles(); bid=124.0; ask=124.02; bids=[(124,10)]; asks=[(124.02,5)]; data_complete=True; orderbook_valid=True
    sigs=SignalFactory().build(S)
    assert all(not x.hard_block for x in sigs)
