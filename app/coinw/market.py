from __future__ import annotations

import asyncio
import time
import httpx

_GRANULARITY = {'1m':'0','5m':'1','15m':'2','1h':'3','4h':'4','1d':'5','1w':'6','3m':'7','30m':'8','1M':'9'}


def _base(instrument: str) -> str:
    value = str(instrument or '').upper().replace('-', '')
    return value[:-4] if value.endswith('USDT') else value


class CoinWMarketClient:
    def __init__(self, base_url='https://api.coinw.com', timeout=6.0):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self._client = None
        self._rate_lock = asyncio.Lock()
        self._last_request = 0.0
        self._cache = {}

    async def close(self):
        if self._client:
            await self._client.aclose()

    async def _get(self, path, params=None):
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        for attempt in range(3):
            async with self._rate_lock:
                await asyncio.sleep(max(0, .14 - (time.monotonic() - self._last_request)))
                self._last_request = time.monotonic()
            try:
                response = await self._client.get(self.base_url + path, params=params or {})
                response.raise_for_status()
                payload = response.json()
                code = str(payload.get('code', '0')) if isinstance(payload, dict) else '0'
                if code not in {'0', '200'}:
                    if code == '29001' and attempt < 2:
                        await asyncio.sleep(.5 * (2 ** attempt))
                        continue
                    message = str(payload.get('msg') or payload.get('message') or '')[:180]
                    raise RuntimeError(f'coinw_public_api_error:{path}:{code}:{message}')
                return payload
            except (httpx.TransportError, httpx.HTTPStatusError):
                if attempt == 2:
                    raise
                await asyncio.sleep(.5 * (2 ** attempt))
        raise RuntimeError('coinw_public_retries_exhausted')

    async def depth(self, instrument):
        return await self._get('/v1/perpumPublic/depth', {'base': _base(instrument)})

    async def klines(self, instrument, period='5m', size=320):
        if period not in _GRANULARITY:
            raise ValueError('unsupported_timeframe')
        key = (_base(instrument), period, size)
        cached = self._cache.get(key)
        # Cache only inside the current candle bucket. Boundary refresh is immediate.
        span = {
            '1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800,
            '1h': 3600, '4h': 14400, '1d': 86400, '1w': 604800, '1M': 2592000,
        }.get(period, 60)
        bucket = int(time.time()) // span
        if cached and cached[0] == bucket and time.monotonic() - cached[1] < 30:
            return cached[2]
        raw = await self._get('/v1/perpumPublic/klines', {
            'currencyCode': key[0], 'granularity': _GRANULARITY[period],
            'klineType': '0', 'limit': min(max(int(size), 1), 1500)})
        self._cache[key] = (bucket, time.monotonic(), raw)
        if len(self._cache) > 500:
            self._cache = {k: v for k, v in self._cache.items() if time.monotonic() - v[1] < 60}
        return raw

    async def ticker(self, instrument):
        return await self._get('/v1/perpumPublic/ticker', {'instrument': _base(instrument)})

    async def tickers(self):
        return await self._get('/v1/perpumPublic/tickers')

    async def instruments(self):
        return await self._get('/v1/perpum/instruments')
