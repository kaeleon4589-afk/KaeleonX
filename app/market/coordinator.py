from __future__ import annotations

import asyncio
import math
import time
from types import SimpleNamespace

from app.models.market import Candle

TF_MS = {'5m': 300_000, '15m': 900_000, '1h': 3_600_000}


class MarketCoordinator:
    def __init__(self, client, symbol, timeframe='5m', signal_factory=None,
                 poll_seconds=2.0, audit=None):
        self.client = client
        self.symbol = symbol
        self.timeframe = timeframe
        self.signal_factory = signal_factory
        self.poll_seconds = poll_seconds
        self.audit = audit

    @staticmethod
    def _parse_klines(raw):
        data = raw.get('data', raw) if isinstance(raw, dict) else raw
        if isinstance(data, dict):
            data = data.get('data', data.get('rows', data.get('list', [])))
        out = {}
        for row in data or []:
            try:
                if isinstance(row, dict):
                    values = [row.get(long, row.get(short)) for long, short in
                              [('open', 'o'), ('high', 'h'), ('low', 'l'), ('close', 'c'), ('volume', 'v')]]
                    ts = int(row.get('timestamp') or row.get('time') or row.get('ts') or row.get('t'))
                else:
                    ts, *values = row[:6]
                    ts = int(ts)
                if ts < 100_000_000_000:
                    ts *= 1000
                o, h, low, c, volume = [float(v or 0) for v in values]
                if (not all(math.isfinite(v) for v in (o, h, low, c, volume))
                        or min(o, h, low, c) <= 0 or volume < 0
                        or low > min(o, c) or h < max(o, c)):
                    continue
                out[ts] = Candle(ts, o, h, low, c, volume)
            except (TypeError, ValueError, IndexError):
                continue
        return sorted(out.values(), key=lambda c: c.timestamp)

    @staticmethod
    def _parse_depth(raw):
        data = raw.get('data', raw) if isinstance(raw, dict) else raw
        if not isinstance(data, dict):
            return [], []

        def parse(rows, reverse):
            levels = []
            for row in rows or []:
                try:
                    if isinstance(row, dict):
                        p = row.get('p', row.get('price'))
                        q = row.get('m', row.get('quantity', row.get('qty')))
                    else:
                        p, q = row[:2]
                    p, q = float(p), float(q)
                    if math.isfinite(p) and math.isfinite(q) and p > 0 and q > 0:
                        levels.append((p, q))
                except (TypeError, ValueError, IndexError):
                    continue
            return sorted(levels, reverse=reverse)

        bids = parse(data.get('bids'), True)
        asks = parse(data.get('asks'), False)
        if not bids or not asks or bids[0][0] > asks[0][0]:
            return [], []
        return bids, asks

    async def quote(self, symbol):
        bids, asks = self._parse_depth(await self.client.depth(symbol))
        if not bids or not asks:
            raise RuntimeError(f'invalid_orderbook:{symbol}')
        bid, ask = bids[0][0], asks[0][0]
        return SimpleNamespace(symbol=symbol, timeframe='5m', candles=[], timeframes={},
                               bid=bid, ask=ask, bids=bids, asks=asks,
                               last=(bid + ask) / 2, quote_received_ms=int(time.time() * 1000),
                               orderbook_valid=True, data_complete=False, monitor_only=True)

    async def snapshot(self, symbol=None, btc_candles=None):
        sym = symbol or self.symbol
        k5, k15, k1h = await asyncio.gather(
            self.client.klines(sym, '5m', 321), self.client.klines(sym, '15m', 241),
            self.client.klines(sym, '1h', 241))
        # Fetch executable quotes after candles, not before potentially slow downloads.
        quote = await self.quote(sym)
        now = int(time.time() * 1000)
        frames = {}
        for tf, raw, minimum in [('5m', k5, 260), ('15m', k15, 200), ('1h', k1h, 200)]:
            span = TF_MS[tf]
            candles = [c for c in self._parse_klines(raw) if c.timestamp + span <= now]
            if len(candles) < minimum:
                raise RuntimeError(f'insufficient_candles:{sym}:{tf}:{len(candles)}')
            recent = candles[-minimum:]
            if now - recent[-1].timestamp > span * 2 + 30_000:
                raise RuntimeError(f'stale_candles:{sym}:{tf}')
            if any(b.timestamp - a.timestamp != span for a, b in zip(recent, recent[1:])):
                raise RuntimeError(f'candle_gap:{sym}:{tf}')
            frames[tf] = candles
        quote.candles = frames['5m']
        quote.timeframes = frames
        quote.btc_candles = btc_candles
        quote.data_complete = True
        quote.monitor_only = False
        return quote

    async def run(self, on_snapshot):
        while True:
            try:
                await on_snapshot(await self.snapshot())
            except Exception as exc:
                if self.audit:
                    self.audit.event('MARKET_LOOP_ERROR', 'system', error=str(exc), symbol=self.symbol)
            await asyncio.sleep(self.poll_seconds)


class MultiMarketCoordinator:
    def __init__(self, client, scanner, poll_seconds=2.0, audit=None, max_parallel=3,
                 heartbeat_seconds=900.0):
        self.client = client
        self.scanner = scanner
        self.poll_seconds = poll_seconds
        self.audit = audit
        self.max_parallel = max_parallel
        self.heartbeat_seconds = heartbeat_seconds
        self._btc = None
        self._cursor = 0
        self._last_heartbeat = 0.0

    async def monitor(self, on_snapshot, symbols_provider):
        """Open risk is monitored even when its symbol leaves the entry shortlist."""
        base = MarketCoordinator(self.client, 'BTC', audit=self.audit)
        while True:
            try:
                symbols = sorted(set(symbols_provider()))
                for symbol in symbols:
                    try:
                        await on_snapshot(await base.quote(symbol))
                    except Exception as exc:
                        if self.audit:
                            self.audit.event('MARKET_SNAPSHOT_ERROR', 'system', symbol=symbol,
                                             error=f'position_monitor:{exc}')
                        # LIVE reconciliation does not require a quote; keep it running.
                        await on_snapshot(SimpleNamespace(symbol=symbol, timeframe='5m', last=None,
                                                          bid=None, ask=None, candles=[], monitor_only=True))
            except Exception as exc:
                if self.audit:
                    self.audit.event('MARKET_LOOP_ERROR', 'system', error=str(exc), symbol='MONITOR')
            await asyncio.sleep(self.poll_seconds)

    async def run(self, on_snapshot):
        base = MarketCoordinator(self.client, 'BTC', audit=self.audit)
        while True:
            started = time.monotonic()
            try:
                ranked = await self.scanner.ranked()
                symbols = list(dict.fromkeys(x['symbol'] for x in ranked)) or ['BTC']
                if started - self._last_heartbeat >= self.heartbeat_seconds:
                    if self.audit:
                        self.audit.event('ENGINE_HEARTBEAT', 'system', market='AUTO_COINW',
                                         markets_scanned=len(symbols), top_symbols=symbols[:5])
                    self._last_heartbeat = started
                try:
                    self._btc = (await base.snapshot('BTC')).candles
                except Exception as exc:
                    self._btc = None  # Never reuse an unbounded stale BTC context.
                    if self.audit:
                        self.audit.event('MARKET_SNAPSHOT_ERROR', 'system', symbol='BTC', error=str(exc))
                batch = symbols[self._cursor:self._cursor + self.max_parallel]
                if not batch:
                    batch = symbols[:self.max_parallel]
                    self._cursor = 0
                self._cursor = (self._cursor + len(batch)) % len(symbols)
                snaps = await asyncio.gather(*(base.snapshot(s, self._btc) for s in batch), return_exceptions=True)
                for symbol, snap in zip(batch, snaps):
                    if isinstance(snap, Exception):
                        if self.audit:
                            self.audit.event('MARKET_SNAPSHOT_ERROR', 'system', symbol=symbol, error=str(snap))
                        continue
                    snap.markets_scanned = len(symbols)
                    snap.candidates = len(ranked)
                    await on_snapshot(snap)
            except Exception as exc:
                if self.audit:
                    self.audit.event('MARKET_LOOP_ERROR', 'system', error=str(exc), symbol='MULTI')
            await asyncio.sleep(max(.1, self.poll_seconds - (time.monotonic() - started)))
