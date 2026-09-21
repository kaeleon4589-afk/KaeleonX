from __future__ import annotations
import time

DEFAULT_BLOCKED = {'DOGE','SHIB','PEPE','BONK','FLOKI','WIF','POPCAT','PENGU','TURBO','MOG','BOME','MYRO','BRETT','NEIRO','MEME','BABYDOGE','KISHU','WOJAK'}


class CoinWMarketScanner:
    def __init__(self, client, depth=12, blocked=None, cache_seconds=30, audit=None):
        self.client = client
        self.depth = depth
        self.blocked = set(blocked or DEFAULT_BLOCKED)
        self.cache_seconds = cache_seconds
        self.audit = audit
        self._cache = []
        self._ts = 0

    @staticmethod
    def _rows(raw):
        data = raw.get('data', raw) if isinstance(raw, dict) else raw
        if isinstance(data, dict):
            for k in ('rows','list','data','tickers','instruments'):
                if isinstance(data.get(k), list):
                    return data[k]
        return data if isinstance(data, list) else []

    async def ranked(self):
        if self._cache and time.time() - self._ts < self.cache_seconds:
            if self.audit:
                self.audit.event('MARKET_SCAN_CACHE_HIT', 'system', level='DEBUG', persist=False,
                                 candidates=len(self._cache), symbols=[x['symbol'] for x in self._cache])
            return list(self._cache)
        started = time.monotonic()
        try:
            rows = self._rows(await self.client.tickers())
        except Exception as exc:
            if self.audit:
                self.audit.event('MARKET_SCAN_ERROR', 'system', error=str(exc))
            rows = []
        scored = []
        rejected = {'blocked': 0, 'invalid': 0, 'zero_liquidity': 0}
        for r in rows:
            if not isinstance(r, dict):
                rejected['invalid'] += 1
                continue
            sym = str(r.get('base_coin') or r.get('base') or r.get('instrument') or r.get('name') or r.get('symbol') or r.get('s') or '').upper().replace('-','')
            if not sym:
                rejected['invalid'] += 1
                continue
            base = sym.replace('USDT','').replace('_PERP','').replace('PERP','')
            if any(x in base for x in self.blocked):
                rejected['blocked'] += 1
                continue
            try:
                last = float(r.get('last_price') or r.get('last') or r.get('lastPrice') or r.get('close') or r.get('price') or 0)
                vol = float(r.get('total_volume') or r.get('amount24h') or r.get('quoteVolume') or r.get('volume') or r.get('vol') or 0)
                ch = float(r.get('rise_fall_rate') or r.get('change24h') or r.get('priceChangePercent') or r.get('change') or 0)
            except Exception:
                rejected['invalid'] += 1
                continue
            if last <= 0 or vol <= 0:
                rejected['zero_liquidity'] += 1
                continue
            if abs(ch) > 1:
                ch /= 100.0
            score = .70 * min(vol / 1_000_000, 1.0) + .30 * min(abs(ch) / .05, 1.0)
            scored.append({'symbol': base, 'score': score, 'volume': vol, 'change_24h': ch, 'price': last})
        scored.sort(key=lambda x: x['score'], reverse=True)
        self._cache = scored[:self.depth]
        self._ts = time.time()
        if self.audit:
            self.audit.event(
                'MARKET_SCAN_DONE', 'system',
                universe_rows=len(rows), eligible=len(scored), shortlisted=len(self._cache),
                rejected=rejected, duration_ms=round((time.monotonic()-started)*1000, 2),
                top=[{'symbol':x['symbol'],'score':round(x['score'],4),'volume':x['volume'],'change_24h':round(x['change_24h'],6)} for x in self._cache],
            )
        return list(self._cache)
