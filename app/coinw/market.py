from __future__ import annotations

import asyncio
import time

import httpx

_GRANULARITY = {'1m': '0', '5m': '1', '15m': '2', '1h': '3', '4h': '4', '1d': '5', '1w': '6', '3m': '7', '30m': '8', '1M': '9'}


def _base(instrument: str) -> str:
    value = str(instrument or '').upper().replace('-', '').replace('_', '')
    return value[:-4] if value.endswith('USDT') else value


class CoinWPublicAPIError(RuntimeError):
    """Preserve the exchange code and failing endpoint without leaking credentials."""

    def __init__(self, code, path: str, message: str = '', http_status=None):
        self.code = str(code)
        self.path = path
        self.http_status = http_status
        self.message = str(message or '')[:160]
        super().__init__(f'coinw_public_api_error:{self.code}:endpoint={path}:{self.message}')

    @property
    def rate_limited(self) -> bool:
        return self.code == '29001' or self.http_status == 429


class CoinWMarketClient:
    """Shared transport with conservative, process-wide public market pacing.

    CoinW publishes 20 klines / 2s, 10 depth / 2s and a shared IP limit.
    One client + one gate is used for this worker's ticker, candles and depth.
    Other applications sharing the same public IP remain outside its control.
    """

    def __init__(self, base_url='https://api.coinw.com', timeout=6.0, max_rps=6.0):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.max_rps = min(10.0, max(1.0, float(max_rps)))
        self._session = None
        self._gate = asyncio.Lock()
        self._next_request = 0.0
        self._cooldown_until = 0.0
        self._backoff_seconds = 0.0

    async def aclose(self):
        async with self._gate:
            session, self._session = self._session, None
        if session is not None:
            await session.aclose()

    async def _paced_session(self):
        async with self._gate:
            now = time.monotonic()
            wait = max(0.0, self._cooldown_until - now, self._next_request - now)
            if wait:
                await asyncio.sleep(wait)
            self._next_request = time.monotonic() + (1.0 / self.max_rps)
            if self._session is None:
                self._session = httpx.AsyncClient(
                    timeout=self.timeout,
                    limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
                )
            return self._session

    async def _get(self, path, params=None):
        session = await self._paced_session()
        try:
            response = await session.get(self.base_url + path, params=params or {})
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            code = str(exc.response.status_code)
            error = CoinWPublicAPIError(code, path, 'http_error', exc.response.status_code)
            if error.rate_limited:
                await self._backoff()
            raise error from exc
        except (httpx.TimeoutException, httpx.TransportError, ValueError) as exc:
            raise CoinWPublicAPIError('transport', path, type(exc).__name__) from exc
        if isinstance(payload, dict) and str(payload.get('code', '0')) not in {'0', '200'}:
            error = CoinWPublicAPIError(payload.get('code'), path, payload.get('msg', ''))
            if error.rate_limited:
                await self._backoff()
            raise error
        # Reset only after a successful response, never on another rejected request.
        self._backoff_seconds = 0.0
        return payload

    async def _backoff(self):
        async with self._gate:
            self._backoff_seconds = min(60.0, max(5.0, self._backoff_seconds * 2))
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + self._backoff_seconds)

    async def depth(self, instrument):
        return await self._get('/v1/perpumPublic/depth', {'base': _base(instrument)})

    async def klines(self, instrument, period='5m', size=320):
        return await self._get('/v1/perpumPublic/klines', {
            'currencyCode': _base(instrument), 'granularity': _GRANULARITY.get(period, '1'),
            'klineType': '0', 'limit': min(max(int(size), 1), 1500),
        })

    async def ticker(self, instrument):
        return await self._get('/v1/perpumPublic/ticker', {'instrument': _base(instrument)})

    async def tickers(self):
        return await self._get('/v1/perpumPublic/tickers')

    async def instruments(self):
        return await self._get('/v1/perpum/instruments')
