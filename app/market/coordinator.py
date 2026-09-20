from __future__ import annotations
import asyncio
import time
from app.models.market import Candle, MarketSnapshot


class MarketCoordinator:
    """REST bootstrap/polling coordinator. WebSocket can later replace polling without changing consumers."""
    def __init__(self, client, symbol, timeframe, signal_factory=None, poll_seconds=2.0, audit=None):
        self.client=client; self.symbol=symbol; self.timeframe=timeframe; self.signal_factory=signal_factory
        self.poll_seconds=poll_seconds; self.audit=audit

    @staticmethod
    def _parse_klines(raw):
        data = raw.get('data', raw) if isinstance(raw, dict) else raw
        if isinstance(data, dict): data = data.get('data', data.get('rows', []))
        candles=[]
        for row in data or []:
            if isinstance(row, dict):
                ts=int(row.get('timestamp') or row.get('time') or row.get('ts'))
                candles.append(Candle(ts,float(row['open']),float(row['high']),float(row['low']),float(row['close']),float(row.get('volume',0))))
            else:
                vals=list(row)
                # CoinW kline ordering is timestamp/open/high/low/close/volume.
                candles.append(Candle(int(vals[0]),float(vals[1]),float(vals[2]),float(vals[3]),float(vals[4]),float(vals[5])))
        return sorted(candles,key=lambda x:x.timestamp)

    @staticmethod
    def _parse_depth(raw):
        data=raw.get('data',raw) if isinstance(raw,dict) else raw
        bids=data.get('bids',[]) if isinstance(data,dict) else []
        asks=data.get('asks',[]) if isinstance(data,dict) else []
        return [(float(x[0]),float(x[1])) for x in bids], [(float(x[0]),float(x[1])) for x in asks]

    async def snapshot(self):
        klines, depth = await asyncio.gather(
            self.client.klines(self.symbol,self.timeframe,200),
            self.client.depth(self.symbol),
        )
        candles=self._parse_klines(klines)
        bids,asks=self._parse_depth(depth)
        bid=bids[0][0] if bids else None; ask=asks[0][0] if asks else None
        return MarketSnapshot(self.symbol,self.timeframe,candles,bid,ask,bool(bids and asks),len(candles)>=30), bids, asks

    async def run(self, on_snapshot):
        while True:
            started=time.monotonic()
            try:
                snap,bids,asks=await self.snapshot()
                snap=MarketSnapshot(snap.symbol,snap.timeframe,snap.candles,snap.bid,snap.ask,snap.orderbook_valid,snap.data_complete)
                # attach book for downstream signal calculation without changing public model.
                snap=type('RuntimeSnapshot',(object,),{'symbol':snap.symbol,'timeframe':snap.timeframe,'candles':snap.candles,'bid':snap.bid,'ask':snap.ask,'orderbook_valid':snap.orderbook_valid,'data_complete':snap.data_complete,'bids':bids,'asks':asks,'last':snap.candles[-1].close})()
                result=on_snapshot(snap)
                if hasattr(result, "__await__"):
                    await result
            except Exception as exc:
                if self.audit: self.audit.event('MARKET_LOOP_ERROR',error=str(exc),symbol=self.symbol)
            await asyncio.sleep(max(0.1,self.poll_seconds-(time.monotonic()-started)))
