from __future__ import annotations
import asyncio,time,math
from app.models.market import Candle,MarketSnapshot

class MarketSnapshotError(RuntimeError):
    """Identify which endpoint/timeframe failed without a full market payload."""

    def __init__(self, symbol, stage, exc):
        self.symbol = symbol
        self.stage = stage
        self.code = getattr(exc, 'code', None)
        self.path = getattr(exc, 'path', None)
        self.http_status = getattr(exc, 'http_status', None)
        super().__init__(f'{stage}:{exc}')


class MarketCoordinator:
    def __init__(self,client,symbol,timeframe='5m',signal_factory=None,poll_seconds=2.0,audit=None):
        self.client=client; self.symbol=symbol; self.timeframe=timeframe; self.signal_factory=signal_factory; self.poll_seconds=poll_seconds; self.audit=audit
        self._candle_cache = {}  # (symbol, timeframe) -> (candles, monotonic timestamp)
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
        """Parse actual CoinW futures depth levels (``p`` price, ``m`` size).

        Prior code expected ``price``/``quantity``, discarded every level of
        CoinW's documented ``{"p": ..., "m": ...}`` response, and made all
        executable signals fail with ``orderbook_valid=false``. Accept generic
        aliases for fixtures and future API adaptations, but never fabricate a
        quote when one side is absent or contains invalid prices/sizes.
        """
        data = raw.get('data', raw) if isinstance(raw, dict) else raw
        if isinstance(data, dict) and isinstance(data.get('data'), dict):
            data = data['data']
        if not isinstance(data, dict):
            return [], []

        def parse(rows, reverse):
            levels = []
            for item in rows if isinstance(rows, (list, tuple)) else []:
                try:
                    if isinstance(item, dict):
                        price = item.get('p')
                        if price is None: price = item.get('price')
                        size = item.get('m')
                        if size is None: size = item.get('quantity')
                        if size is None: size = item.get('qty')
                    else:
                        price, size = item[0], item[1]
                    price, size = float(price), float(size)
                    if math.isfinite(price) and math.isfinite(size) and price > 0 and size > 0:
                        levels.append((price, size))
                except (TypeError, ValueError, IndexError, KeyError):
                    continue
            # Using the true best quotes protects execution if depth arrives
            # unsorted; an empty or crossed book is still rejected downstream.
            return sorted(levels, key=lambda level: level[0], reverse=reverse)

        return parse(data.get('bids'), True), parse(data.get('asks'), False)
    async def _candles(self, symbol, timeframe, size, ttl):
        key = (symbol, timeframe)
        cached = self._candle_cache.get(key)
        age = time.monotonic() - cached[1] if cached else float('inf')
        if cached and ttl and age < ttl:
            return cached[0]
        try:
            candles = self._parse_klines(await self.client.klines(symbol, timeframe, size))
            if not candles:
                raise ValueError(f'empty_klines:{timeframe}')
        except Exception as exc:
            # Only closed higher-timeframe context may use a brief cache grace.
            # Never reuse failed 5m execution candles or an old depth quote.
            if cached and ttl and age < ttl * 2:
                if self.audit:
                    self.audit.event('MARKET_CANDLE_CACHE_FALLBACK', 'system', level='DEBUG',
                                     symbol=symbol, timeframe=timeframe, age_seconds=round(age, 1))
                return cached[0]
            raise MarketSnapshotError(symbol, timeframe, exc) from exc
        self._candle_cache[key] = (candles, time.monotonic())
        return candles

    async def _depth(self, symbol):
        try:
            return self._parse_depth(await self.client.depth(symbol))
        except Exception as exc:
            # Fail closed for execution, while retaining valid candle analysis.
            if self.audit:
                self.audit.event('MARKET_DEPTH_ERROR', 'system', symbol=symbol,
                                 error=str(exc), api_code=getattr(exc, 'code', None),
                                 endpoint=getattr(exc, 'path', '/v1/perpumPublic/depth'))
            return [], []

    async def snapshot(self, symbol=None, btc_candles=None):
        sym = symbol or self.symbol
        c5, c15, c1h, depth = await asyncio.gather(
            self._candles(sym, '5m', 320, 0.0),
            self._candles(sym, '15m', 240, 60.0),
            self._candles(sym, '1h', 240, 180.0),
            self._depth(sym),
        )
        bids, asks = depth
        bid = bids[0][0] if bids else None
        ask = asks[0][0] if asks else None
        return type('RuntimeSnapshot', (object,), {
            'symbol': sym, 'timeframe': '5m', 'candles': c5,
            'timeframes': {'5m': c5, '15m': c15, '1h': c1h},
            'btc_candles': btc_candles, 'bid': bid, 'ask': ask,
            'orderbook_valid': bool(bids and asks and bid < ask),
            'data_complete': len(c5) >= 260 and len(c15) >= 100 and len(c1h) >= 100,
            'bids': bids, 'asks': asks, 'last': c5[-1].close,
        })()
    async def run(self,on_snapshot):
        while True:
            started=time.monotonic()
            try:
                snap=await self.snapshot(); r=on_snapshot(snap); await r if hasattr(r,'__await__') else None
            except Exception as exc:
                if self.audit:self.audit.event('MARKET_LOOP_ERROR','system',error=str(exc),symbol=self.symbol)
            await asyncio.sleep(max(.1,self.poll_seconds-(time.monotonic()-started)))

class MultiMarketCoordinator:
    def __init__(self, client, scanner, poll_seconds=2.0, audit=None, max_parallel=3, heartbeat_seconds=900.0):
        self.client = client
        self.scanner = scanner
        self.poll_seconds = poll_seconds
        self.audit = audit
        self.max_parallel = max_parallel
        self.heartbeat_seconds = heartbeat_seconds
        self._btc = None
        self._btc_at = 0.0
        self._cursor = 0
        self._last_heartbeat = 0.0
        self._symbol_cooldown = {}

    async def _btc_context(self):
        # BTC context is only 5m candles; it must not require BTC's full
        # snapshot, 15m/1h history or order book every two seconds.
        now = time.monotonic()
        if self._btc is not None and now - self._btc_at < 60.0:
            return self._btc
        try:
            candles = MarketCoordinator._parse_klines(await self.client.klines('BTC', '5m', 320))
            if not candles:
                raise ValueError('btc_candles_empty')
            self._btc = candles
            self._btc_at = time.monotonic()
        except Exception as exc:
            if self.audit:
                self.audit.event('BTC_CONTEXT_ERROR', 'system', level='WARNING', error=str(exc))
            if now - self._btc_at > 300:
                self._btc = None
        return self._btc

    async def run(self, on_snapshot):
        base = MarketCoordinator(self.client, 'BTC', poll_seconds=self.poll_seconds, audit=self.audit)
        while True:
            started = time.monotonic()
            try:
                ranked = await self.scanner.ranked()
                all_symbols = [x['symbol'] for x in ranked] or ['BTC']
                now = time.monotonic()
                if self.audit and (self._last_heartbeat == 0.0 or now - self._last_heartbeat >= self.heartbeat_seconds):
                    self.audit.event('ENGINE_HEARTBEAT', 'system', market='AUTO_COINW',
                                     markets_scanned=len(all_symbols), top_symbols=all_symbols[:5])
                    self._last_heartbeat = now
                symbols = [sym for sym in all_symbols if self._symbol_cooldown.get(sym, 0.0) <= now]
                if not symbols:
                    await asyncio.sleep(max(0.1, self.poll_seconds))
                    continue
                await self._btc_context()
                self._cursor %= len(symbols)
                batch = symbols[self._cursor:self._cursor + self.max_parallel]
                if not batch:
                    self._cursor = 0
                    batch = symbols[:self.max_parallel]
                self._cursor = (self._cursor + len(batch)) % len(symbols)
                if self.audit:
                    self.audit.event('MARKET_BATCH_START', 'system', level='DEBUG', persist=False, batch=batch)
                snaps = await asyncio.gather(*(base.snapshot(s, self._btc) for s in batch), return_exceptions=True)
                for requested_symbol, snap in zip(batch, snaps):
                    if isinstance(snap, Exception):
                        # A bad/delisted symbol should not consume the next batch
                        # over and over; retry automatically after a brief rest.
                        api_code = getattr(snap, 'code', None)
                        self._symbol_cooldown[requested_symbol] = time.monotonic() + (
                            15 if str(api_code) == '29001' else 60
                        )
                        if self.audit:
                            self.audit.event('MARKET_SNAPSHOT_ERROR', 'system',
                                             symbol=requested_symbol, error=str(snap),
                                             stage=getattr(snap, 'stage', 'unknown'),
                                             api_code=api_code,
                                             endpoint=getattr(snap, 'path', None))
                        continue
                    snap.markets_scanned = len(all_symbols)
                    snap.candidates = len(ranked)
                    snap.scan_batch = list(batch)
                    result = on_snapshot(snap)
                    if hasattr(result, '__await__'):
                        await result
            except Exception as exc:
                if self.audit:
                    self.audit.event('MARKET_LOOP_ERROR', 'system', error=str(exc), symbol='MULTI')
            await asyncio.sleep(max(0.1, self.poll_seconds - (time.monotonic() - started)))
