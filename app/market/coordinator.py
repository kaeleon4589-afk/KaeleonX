from __future__ import annotations
import asyncio,time
from app.models.market import Candle,MarketSnapshot

class MarketCoordinator:
    def __init__(self,client,symbol,timeframe='5m',signal_factory=None,poll_seconds=2.0,audit=None):
        self.client=client; self.symbol=symbol; self.timeframe=timeframe; self.signal_factory=signal_factory; self.poll_seconds=poll_seconds; self.audit=audit
    @staticmethod
    def _parse_klines(raw):
        data=raw.get('data',raw) if isinstance(raw,dict) else raw
        if isinstance(data,dict): data=data.get('data',data.get('rows',data.get('list',[])))
        out=[]
        for row in data or []:
            try:
                if isinstance(row,dict):
                    ts=int(row.get('timestamp') or row.get('time') or row.get('ts') or row.get('t')); out.append(Candle(ts,float(row.get('open') or row.get('o')),float(row.get('high') or row.get('h')),float(row.get('low') or row.get('l')),float(row.get('close') or row.get('c')),float(row.get('volume') or row.get('v') or 0)))
                else:
                    v=list(row); out.append(Candle(int(v[0]),float(v[1]),float(v[2]),float(v[3]),float(v[4]),float(v[5] if len(v)>5 else 0)))
            except Exception: continue
        return sorted(out,key=lambda x:x.timestamp)
    @staticmethod
    def _parse_depth(raw):
        data=raw.get('data',raw) if isinstance(raw,dict) else raw; bids=data.get('bids',[]) if isinstance(data,dict) else []; asks=data.get('asks',[]) if isinstance(data,dict) else []
        def parse(xs):
            out=[]
            for x in xs:
                try: out.append((float(x[0] if not isinstance(x,dict) else x.get('price')),float(x[1] if not isinstance(x,dict) else x.get('quantity',x.get('qty')))))
                except Exception: pass
            return out
        return parse(bids),parse(asks)
    async def snapshot(self,symbol=None,btc_candles=None):
        sym=symbol or self.symbol
        k5,k15,k1h,depth=await asyncio.gather(self.client.klines(sym,'5m',320),self.client.klines(sym,'15m',240),self.client.klines(sym,'1h',240),self.client.depth(sym))
        c5=self._parse_klines(k5); c15=self._parse_klines(k15); c1h=self._parse_klines(k1h); bids,asks=self._parse_depth(depth); bid=bids[0][0] if bids else None; ask=asks[0][0] if asks else None
        if not c5: raise RuntimeError(f'no_candles:{sym}')
        return type('RuntimeSnapshot',(object,),{'symbol':sym,'timeframe':'5m','candles':c5,'timeframes':{'5m':c5,'15m':c15,'1h':c1h},'btc_candles':btc_candles,'bid':bid,'ask':ask,'orderbook_valid':bool(bids and asks),'data_complete':len(c5)>=260 and len(c15)>=100 and len(c1h)>=100,'bids':bids,'asks':asks,'last':c5[-1].close})()
    async def run(self,on_snapshot):
        while True:
            started=time.monotonic()
            try:
                snap=await self.snapshot(); r=on_snapshot(snap); await r if hasattr(r,'__await__') else None
            except Exception as exc:
                if self.audit:self.audit.event('MARKET_LOOP_ERROR','system',error=str(exc),symbol=self.symbol)
            await asyncio.sleep(max(.1,self.poll_seconds-(time.monotonic()-started)))

class MultiMarketCoordinator:
    def __init__(self,client,scanner,poll_seconds=2.0,audit=None,max_parallel=3): self.client=client; self.scanner=scanner; self.poll_seconds=poll_seconds; self.audit=audit; self.max_parallel=max_parallel; self._btc=None; self._cursor=0
    async def run(self,on_snapshot):
        base=MarketCoordinator(self.client,'BTC',poll_seconds=self.poll_seconds,audit=self.audit)
        while True:
            started=time.monotonic()
            try:
                ranked=await self.scanner.ranked(); symbols=[x['symbol'] for x in ranked] or ['BTC']
                if self.audit:
                    self.audit.event('MARKET_BATCH_SELECTED','system',level='DEBUG',persist=False,universe_size=len(symbols),cursor=self._cursor,max_parallel=self.max_parallel,symbols=symbols)
                try: self._btc=(await base.snapshot('BTC')).candles
                except Exception as exc:
                    if self.audit:self.audit.event('BTC_CONTEXT_ERROR','system',level='WARNING',error=str(exc))
                batch=symbols[self._cursor:self._cursor+self.max_parallel]
                if not batch: self._cursor=0; batch=symbols[:self.max_parallel]
                self._cursor=(self._cursor+len(batch))%max(len(symbols),1)
                if self.audit:self.audit.event('MARKET_BATCH_START','system',level='DEBUG',persist=False,batch=batch)
                snaps=await asyncio.gather(*(base.snapshot(s,self._btc) for s in batch),return_exceptions=True)
                for snap in snaps:
                    if isinstance(snap,Exception):
                        if self.audit:self.audit.event('MARKET_SNAPSHOT_ERROR','system',error=str(snap)); continue
                    snap.markets_scanned=len(symbols)
                    snap.candidates=len(ranked)
                    snap.scan_batch=list(batch)
                    r=on_snapshot(snap); await r if hasattr(r,'__await__') else None
            except Exception as exc:
                if self.audit:self.audit.event('MARKET_LOOP_ERROR','system',error=str(exc),symbol='MULTI')
            await asyncio.sleep(max(.1,self.poll_seconds-(time.monotonic()-started)))
