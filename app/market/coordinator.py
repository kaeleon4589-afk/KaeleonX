from __future__ import annotations

import asyncio
import math
import time
from types import SimpleNamespace

from app.models.market import Candle

TF_MS = {'1m': 60_000, '5m': 300_000, '15m': 900_000, '1h': 3_600_000}


class MarketCoordinator:
    def __init__(self, client, symbol, timeframe='5m', signal_factory=None,
                 poll_seconds=2.0, audit=None):
        self.client = client
        self.symbol = symbol
        self.timeframe = timeframe
        self.signal_factory = signal_factory
        self.poll_seconds = poll_seconds
        self.audit = audit
        # ARMED monitoring refreshes closed 5m trend context on a bounded cadence.
        # The quote/1m path remains high-frequency; 5m is cached per symbol so
        # trigger-time trend revalidation does not multiply CoinW traffic.
        self._armed_trend_cache = {}
        self._armed_trend_cache_ttl_seconds = 15.0

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

    async def _armed_trend_candles(self, symbol):
        now_mono = time.monotonic()
        cached = self._armed_trend_cache.get(symbol)
        if cached and now_mono - float(cached[0]) < self._armed_trend_cache_ttl_seconds:
            return list(cached[1])
        raw = await self.client.klines(symbol, '5m', 321)
        now_ms = int(time.time() * 1000)
        span = TF_MS['5m']
        candles = [c for c in self._parse_klines(raw) if c.timestamp + span <= now_ms]
        if len(candles) < 205:
            raise RuntimeError(f'insufficient_armed_trend_candles:{symbol}:{len(candles)}')
        recent = candles[-205:]
        fresh = now_ms - recent[-1].timestamp <= span * 2 + 30_000
        contiguous = all(b.timestamp - a.timestamp == span for a, b in zip(recent, recent[1:]))
        if not fresh:
            raise RuntimeError(f'stale_armed_trend_candles:{symbol}')
        if not contiguous:
            raise RuntimeError(f'armed_trend_candle_gap:{symbol}')
        self._armed_trend_cache[symbol] = (now_mono, candles)
        return list(candles)

    async def armed_snapshot(self, symbol):
        """Lightweight high-priority snapshot for an already ARMED setup.

        Discovery still uses the full 5m/15m/1h snapshot. After a setup has passed
        those filters, triggering needs an executable order-book quote, a fresh
        CLOSED 1m candle, and the same 321-bar 5m history depth used by discovery.
        Keeping the 5m depth identical prevents EMA200/trend-bias drift caused only
        by re-seeding EMA calculations from a shorter history during ARMED polling.
        """
        quote_result, micro_result, trend_result = await asyncio.gather(
            self.quote(symbol), self.client.klines(symbol, '1m', 61),
            self._armed_trend_candles(symbol),
            return_exceptions=True,
        )
        if isinstance(quote_result, Exception):
            raise quote_result
        quote = quote_result
        frames = {}
        if isinstance(micro_result, Exception):
            if self.audit:
                self.audit.event(
                    'MARKET_MICRODATA_ERROR', 'system', level='WARNING',
                    symbol=symbol, error=f'{type(micro_result).__name__}: {micro_result}',
                )
        else:
            now = int(time.time() * 1000)
            span = TF_MS['1m']
            micro = [c for c in self._parse_klines(micro_result) if c.timestamp + span <= now]
            if len(micro) >= 20:
                recent = micro[-min(30, len(micro)):]
                fresh = now - recent[-1].timestamp <= span * 2 + 15_000
                contiguous = all(b.timestamp - a.timestamp == span for a, b in zip(recent, recent[1:]))
                if fresh and contiguous:
                    frames['1m'] = micro
                elif self.audit:
                    self.audit.event(
                        'MARKET_MICRODATA_INVALID', 'system', level='WARNING',
                        symbol=symbol, bars=len(micro), fresh=fresh, contiguous=contiguous,
                    )
        if isinstance(trend_result, Exception):
            if self.audit:
                self.audit.event(
                    'MARKET_TREND_CONTEXT_ERROR', 'system', level='WARNING',
                    symbol=symbol, error=f'{type(trend_result).__name__}: {trend_result}',
                )
        else:
            frames['5m'] = list(trend_result)
        quote.candles = list(frames.get('5m') or [])
        quote.timeframes = frames
        quote.data_complete = bool(frames.get('1m'))
        quote.trend_context_valid = bool(frames.get('5m'))
        quote.monitor_only = True
        quote.armed_monitor = True
        return quote

    async def watch_snapshot(self, symbol):
        """Full closed-structure snapshot for a precursor already under WATCHING.

        WATCHING is intentionally slower than ARMED monitoring because it only
        needs new closed 5m/15m/1h structure, not tick-level confirmation.
        """
        snap = await self.snapshot(symbol)
        snap.monitor_only = True
        snap.watch_monitor = True
        snap.armed_monitor = False
        return snap

    async def snapshot(self, symbol=None, btc_candles=None):
        sym = symbol or self.symbol
        k5, k15, k1h = await asyncio.gather(
            self.client.klines(sym, '5m', 321), self.client.klines(sym, '15m', 241),
            self.client.klines(sym, '1h', 241))
        # v6 uses closed 1m candles only for micro-confirmation. Keep this feed
        # optional at the market-adapter boundary so monitoring/reconciliation
        # remains available during a temporary 1m endpoint failure.
        try:
            k1m = await self.client.klines(sym, '1m', 181)
        except Exception as exc:
            k1m = None
            if self.audit:
                self.audit.event('MARKET_MICRODATA_ERROR', 'system', level='WARNING',
                                 symbol=sym, error=f'{type(exc).__name__}: {exc}')
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
        if k1m is not None:
            span = TF_MS['1m']
            micro = [c for c in self._parse_klines(k1m) if c.timestamp + span <= now]
            if len(micro) >= 120:
                recent_micro = micro[-120:]
                fresh = now - recent_micro[-1].timestamp <= span * 2 + 15_000
                contiguous = all(b.timestamp - a.timestamp == span for a, b in zip(recent_micro, recent_micro[1:]))
                if fresh and contiguous:
                    frames['1m'] = micro
                elif self.audit:
                    self.audit.event('MARKET_MICRODATA_INVALID', 'system', level='WARNING',
                                     symbol=sym, bars=len(micro), fresh=fresh, contiguous=contiguous)
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
                 heartbeat_seconds=900.0, armed_poll_seconds=None, watching_poll_seconds=15.0):
        self.client = client
        self.scanner = scanner
        self.poll_seconds = poll_seconds
        self.audit = audit
        self.max_parallel = max_parallel
        self.heartbeat_seconds = heartbeat_seconds
        self.armed_poll_seconds = float(armed_poll_seconds if armed_poll_seconds is not None else poll_seconds)
        self.watching_poll_seconds = float(watching_poll_seconds)
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

    async def monitor_armed(self, on_snapshot, symbols_provider):
        """Prioritise ARMED symbols independently from the discovery rotation."""
        base = MarketCoordinator(self.client, 'BTC', audit=self.audit)
        while True:
            started = time.monotonic()
            try:
                symbols = sorted(set(symbols_provider()))
                if symbols:
                    snaps = await asyncio.gather(
                        *(base.armed_snapshot(symbol) for symbol in symbols),
                        return_exceptions=True,
                    )
                    for symbol, snap in zip(symbols, snaps):
                        if isinstance(snap, Exception):
                            if self.audit:
                                self.audit.event(
                                    'MARKET_SNAPSHOT_ERROR', 'system', symbol=symbol,
                                    error=f'armed_monitor:{type(snap).__name__}:{snap}',
                                )
                            continue
                        await on_snapshot(snap)
            except Exception as exc:
                if self.audit:
                    self.audit.event('MARKET_LOOP_ERROR', 'system', error=str(exc), symbol='ARMED_MONITOR')
            await asyncio.sleep(max(.1, self.armed_poll_seconds - (time.monotonic() - started)))

    async def monitor_watching(self, on_snapshot, symbols_provider):
        """Follow BREAKOUT/LIQUIDITY precursors independently from scanner rotation."""
        base = MarketCoordinator(self.client, 'BTC', audit=self.audit)
        while True:
            started = time.monotonic()
            try:
                symbols = sorted(set(symbols_provider()))
                if symbols:
                    snaps = await asyncio.gather(
                        *(base.watch_snapshot(symbol) for symbol in symbols),
                        return_exceptions=True,
                    )
                    for symbol, snap in zip(symbols, snaps):
                        if isinstance(snap, Exception):
                            if self.audit:
                                self.audit.event(
                                    'MARKET_SNAPSHOT_ERROR', 'system', symbol=symbol,
                                    error=f'watch_monitor:{type(snap).__name__}:{snap}',
                                )
                            continue
                        await on_snapshot(snap)
            except Exception as exc:
                if self.audit:
                    self.audit.event('MARKET_LOOP_ERROR', 'system', error=str(exc), symbol='WATCH_MONITOR')
            await asyncio.sleep(max(.1, self.watching_poll_seconds - (time.monotonic() - started)))

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
