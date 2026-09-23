from __future__ import annotations

import os
import time

DEFAULT_BLOCKED = {
    'DOGE','SHIB','PEPE','BONK','FLOKI','WIF','POPCAT','PENGU','TURBO','MOG','BOME','MYRO',
    'BRETT','NEIRO','MEME','BABYDOGE','KISHU','WOJAK','PONKE','MEW','TRUMP','MAGA',
    'HARRYPOTTEROBAMA','HYPE',
}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


class CoinWMarketScanner:
    """CoinW market scanner adapted from Trading-X-Hiper-Pro.

    The reference engine ranks candidates by notional volume (50%), open interest
    (30%) and *directional* 24h trend (20%).  CoinW field names vary by endpoint,
    so this adapter accepts the common aliases while preserving the source score.

    A short normal cache avoids unnecessary public-API traffic.  If a refresh
    fails, the last known-good shortlist remains usable for up to five minutes,
    matching the source bot's scanner fail-safe rather than collapsing the engine
    to an empty universe on a transient CoinW error.
    """

    def __init__(self, client, depth=12, blocked=None, cache_seconds=30, audit=None):
        self.client = client
        self.depth = depth
        self.blocked = set(blocked or DEFAULT_BLOCKED)
        self.cache_seconds = max(float(cache_seconds), 0.0)
        self.failsafe_seconds = max(_env_float('MARKET_SCANNER_FAILSAFE_SECONDS', 300.0), self.cache_seconds)
        self.audit = audit
        self._cache: list[dict] = []
        self._ts = 0.0

    @staticmethod
    def _rows(raw):
        data = raw.get('data', raw) if isinstance(raw, dict) else raw
        if isinstance(data, dict):
            for key in ('rows', 'list', 'data', 'tickers', 'instruments'):
                if isinstance(data.get(key), list):
                    return data[key]
        return data if isinstance(data, list) else []

    @staticmethod
    def _float(row: dict, *keys: str, default: float = 0.0) -> float:
        for key in keys:
            value = row.get(key)
            if value not in (None, ''):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return float(default)

    @classmethod
    def _change_percent(cls, row: dict) -> float:
        raw = cls._float(row, 'rise_fall_rate', 'change24h', 'priceChangePercent', 'change', default=0.0)
        # Existing CoinW integration represents fractional changes such as 0.03
        # as 3%.  Some ticker variants already return percent units such as 3.0.
        return raw * 100.0 if abs(raw) <= 1.0 else raw

    @classmethod
    def _score_row(cls, row: dict):
        symbol = str(
            row.get('base_coin') or row.get('base') or row.get('instrument') or
            row.get('name') or row.get('symbol') or row.get('s') or ''
        ).upper().replace('-', '')
        if not symbol:
            return None

        base = symbol.replace('USDT', '').replace('_PERP', '').replace('PERP', '')
        if not base:
            return None

        last = cls._float(row, 'last_price', 'last', 'lastPrice', 'close', 'price', 'markPx', default=0.0)
        volume = cls._float(row, 'total_volume', 'amount24h', 'quoteVolume', 'volume', 'vol', 'dayNtlVlm', default=0.0)
        oi = cls._float(row, 'openInterest', 'open_interest', 'open_interest_value', 'oi', default=0.0)
        change_pct = cls._change_percent(row)
        if last <= 0.0 or volume <= 0.0:
            return None

        vol_score = min(volume / 1_000_000.0, 1.0)
        oi_score = min(oi / 10_000_000.0, 1.0) if oi > 0.0 else 0.3
        trend_score = max(min((change_pct / 5.0) + 0.5, 1.0), 0.0)
        score = (vol_score * 0.5) + (oi_score * 0.3) + (trend_score * 0.2)

        return {
            'symbol': base,
            'score': round(score, 4),
            'volume': round(volume, 2),
            'oi': round(oi, 2),
            'change_24h': round(change_pct, 2),
            'price': last,
        }

    async def ranked(self):
        now = time.time()
        if self._cache and now - self._ts < self.cache_seconds:
            if self.audit:
                self.audit.event(
                    'MARKET_SCAN_CACHE_HIT', 'system', level='DEBUG', persist=False,
                    candidates=len(self._cache), symbols=[x['symbol'] for x in self._cache],
                )
            return list(self._cache)

        started = time.monotonic()
        fetch_error = None
        try:
            rows = self._rows(await self.client.tickers())
        except Exception as exc:
            fetch_error = exc
            rows = []
            if self.audit:
                self.audit.event('MARKET_SCAN_ERROR', 'system', error=str(exc))

        scored = []
        rejected = {'blocked': 0, 'invalid': 0, 'zero_liquidity': 0}
        for row in rows:
            if not isinstance(row, dict):
                rejected['invalid'] += 1
                continue

            raw_symbol = str(
                row.get('base_coin') or row.get('base') or row.get('instrument') or
                row.get('name') or row.get('symbol') or row.get('s') or ''
            ).upper().replace('-', '')
            if not raw_symbol:
                rejected['invalid'] += 1
                continue
            base = raw_symbol.replace('USDT', '').replace('_PERP', '').replace('PERP', '')
            if any(token in base for token in self.blocked):
                rejected['blocked'] += 1
                continue

            parsed = self._score_row(row)
            if parsed is None:
                last = self._float(row, 'last_price', 'last', 'lastPrice', 'close', 'price', 'markPx', default=0.0)
                volume = self._float(row, 'total_volume', 'amount24h', 'quoteVolume', 'volume', 'vol', 'dayNtlVlm', default=0.0)
                rejected['zero_liquidity' if last <= 0.0 or volume <= 0.0 else 'invalid'] += 1
                continue
            scored.append(parsed)

        if scored:
            scored.sort(key=lambda item: item['score'], reverse=True)
            self._cache = scored[:self.depth]
            self._ts = now
            if self.audit:
                self.audit.event(
                    'MARKET_SCAN_DONE', 'system',
                    universe_rows=len(rows), eligible=len(scored), shortlisted=len(self._cache),
                    rejected=rejected, duration_ms=round((time.monotonic() - started) * 1000, 2),
                    top=[
                        {
                            'symbol': item['symbol'], 'score': item['score'], 'volume': item['volume'],
                            'oi': item['oi'], 'change_24h': item['change_24h'],
                        }
                        for item in self._cache
                    ],
                )
            return list(self._cache)

        # Reference scanner fail-safe: a transient API/empty-universe response
        # must not wipe a healthy shortlist immediately.
        cache_age = now - self._ts if self._ts else float('inf')
        if self._cache and cache_age <= self.failsafe_seconds:
            if self.audit:
                self.audit.event(
                    'MARKET_SCAN_FAILSAFE_CACHE', 'system', level='WARNING', persist=False,
                    candidates=len(self._cache), cache_age_seconds=round(cache_age, 2),
                    reason='ticker_error' if fetch_error is not None else 'empty_live_scan',
                )
            return list(self._cache)

        return []
