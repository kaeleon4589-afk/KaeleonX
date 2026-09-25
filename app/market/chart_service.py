from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Any

from app.coinw.market import CoinWMarketClient

CHART_TIMEFRAMES = {"1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d"}


def canonical_symbol(value: str | None, quote: str = "USDT") -> str:
    """Return a stable UI symbol while preserving CoinW USDC notation."""
    raw = str(value or "").strip().upper().replace("-", "_").replace("/", "_")
    if not raw:
        raise ValueError("invalid_market_symbol")
    raw = "_".join(part for part in raw.split("_") if part)
    if raw.endswith("_USDC"):
        return raw
    if raw.endswith("USDC") and "_" not in raw:
        return f"{raw[:-4]}_USDC"
    if raw.endswith("_USDT"):
        return raw[:-5] + "USDT"
    if raw.endswith("USDT"):
        return raw
    if "_" in raw:
        return raw
    return f"{raw}{quote.upper()}"


def coinw_pair_code(symbol: str) -> str:
    symbol = canonical_symbol(symbol)
    if symbol.endswith("USDT"):
        return symbol[:-4]
    return symbol


def display_symbol(symbol: str) -> str:
    symbol = canonical_symbol(symbol)
    if symbol.endswith("USDT"):
        return f"{symbol[:-4]}/USDT"
    if symbol.endswith("_USDC"):
        return f"{symbol[:-5]}/USDC"
    return symbol.replace("_", "/")


def _finite_number(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non_finite_market_value")
    return number


def normalize_rest_candle(row: Any) -> dict[str, float | int] | None:
    """Normalize CoinW REST candles.

    CoinW's published docs currently describe positional rows as
    [timestamp, high, open, low, close, volume], while older KAELEON market
    code and some historical responses use [timestamp, open, high, low,
    close, volume]. We validate OHLC geometry and accept either ordering so
    the chart cannot silently swap open/high values.
    """
    try:
        if isinstance(row, dict):
            ts = int(row.get("timestamp") or row.get("time") or row.get("ts") or row.get("t"))
            open_price = _finite_number(row.get("open", row.get("o")))
            high = _finite_number(row.get("high", row.get("h")))
            low = _finite_number(row.get("low", row.get("l")))
            close = _finite_number(row.get("close", row.get("c")))
            volume = max(0.0, _finite_number(row.get("volume", row.get("v", 0))))
        else:
            ts = int(row[0])
            first = _finite_number(row[1])
            second = _finite_number(row[2])
            low = _finite_number(row[3])
            close = _finite_number(row[4])
            volume = max(0.0, _finite_number(row[5]))

            documented = (second, first)  # open, high
            legacy = (first, second)      # open, high
            open_price, high = documented
            if not (high >= max(open_price, close) and low <= min(open_price, close)):
                open_price, high = legacy

        if ts < 100_000_000_000:
            ts *= 1000
        if min(open_price, high, low, close) <= 0:
            return None
        if high < max(open_price, close) or low > min(open_price, close) or high < low:
            return None
        return {
            "timestamp": ts,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    except (TypeError, ValueError, IndexError, KeyError):
        return None



def _unwrap_data(raw: Any) -> Any:
    return raw.get("data", raw) if isinstance(raw, dict) else raw


def normalize_depth(raw: Any, limit: int = 20) -> dict[str, Any]:
    data = _unwrap_data(raw)
    if not isinstance(data, dict):
        return {"asks": [], "bids": [], "timestamp": None}

    def side_rows(value: Any, reverse: bool) -> list[dict[str, float]]:
        rows: list[dict[str, float]] = []
        if not isinstance(value, list):
            return rows
        for item in value:
            try:
                if isinstance(item, dict):
                    price = _finite_number(item.get("p", item.get("price")))
                    quantity = max(0.0, _finite_number(item.get("m", item.get("quantity", 0))))
                else:
                    price = _finite_number(item[0])
                    quantity = max(0.0, _finite_number(item[1]))
                if price > 0 and quantity >= 0:
                    rows.append({"price": price, "quantity": quantity})
            except (TypeError, ValueError, IndexError):
                continue
        rows.sort(key=lambda row: row["price"], reverse=reverse)
        return rows[: max(1, min(int(limit), 100))]

    timestamp = data.get("ts") or data.get("timestamp") or data.get("time")
    try:
        timestamp = int(timestamp) if timestamp is not None else None
    except (TypeError, ValueError):
        timestamp = None
    return {
        "asks": side_rows(data.get("ask", data.get("asks")), reverse=False),
        "bids": side_rows(data.get("bids", data.get("bid")), reverse=True),
        "timestamp": timestamp,
    }


def normalize_trades(raw: Any, limit: int = 40) -> list[dict[str, Any]]:
    data = _unwrap_data(raw)
    if isinstance(data, dict):
        for key in ("data", "rows", "list", "items"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            price = _finite_number(item.get("price"))
            quantity = max(0.0, _finite_number(item.get("quantity", item.get("m", 0))))
            timestamp = int(item.get("createdDate") or item.get("timestamp") or item.get("ts") or 0)
            direction = str(item.get("direction") or "").lower()
            if direction not in {"long", "short"}:
                direction = "unknown"
            if price <= 0:
                continue
            rows.append({
                "id": str(item.get("id") or f"{timestamp}:{price}:{quantity}"),
                "timestamp": timestamp,
                "price": price,
                "quantity": quantity,
                "piece": float(item.get("piece") or 0),
                "direction": direction,
            })
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda row: row["timestamp"], reverse=True)
    return rows[: max(1, min(int(limit), 100))]


def normalize_ticker(raw: Any) -> dict[str, Any]:
    data = _unwrap_data(raw)
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return {}

    def optional_number(*keys: str) -> float | None:
        for key in keys:
            if data.get(key) is None:
                continue
            try:
                return _finite_number(data[key])
            except (TypeError, ValueError):
                continue
        return None

    return {
        "last": optional_number("last", "last_price", "price"),
        "index_price": optional_number("fair_price", "indexPrice", "index_price"),
        "high": optional_number("high"),
        "low": optional_number("low"),
        "open": optional_number("open"),
        "change_rate": optional_number("changeRate", "rise_fall_rate", "rose"),
        "volume": optional_number("vol", "total_volume"),
        "volume_quote": optional_number("volUsdt", "volValue"),
        "max_leverage": optional_number("max_leverage", "maxLeverage"),
        "contract_size": optional_number("contract_size", "contractSize"),
    }


def normalize_funding(raw: Any) -> dict[str, Any]:
    data = _unwrap_data(raw)
    if not isinstance(data, dict):
        return {}
    try:
        rate = _finite_number(data.get("value", data.get("r")))
    except (TypeError, ValueError):
        rate = None
    timestamp = data.get("ts") or data.get("timestamp")
    try:
        timestamp = int(timestamp) if timestamp is not None else None
    except (TypeError, ValueError):
        timestamp = None
    return {"rate": rate, "timestamp": timestamp, "kind": "last_settlement"}


@dataclass(frozen=True)
class MarketInstrument:
    symbol: str
    display: str
    base: str
    quote: str
    pair_code: str
    price_precision: int
    status: str
    icon_url: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "display": self.display,
            "base": self.base,
            "quote": self.quote,
            "pair_code": self.pair_code,
            "price_precision": self.price_precision,
            "status": self.status,
            "icon_url": self.icon_url,
        }


class ChartMarketService:
    def __init__(self, client: CoinWMarketClient | None = None, instruments_ttl: float = 60.0):
        self.client = client or CoinWMarketClient()
        self.instruments_ttl = max(5.0, float(instruments_ttl))
        self._instrument_cache: tuple[float, list[MarketInstrument]] | None = None
        self._instrument_lock = asyncio.Lock()

    @staticmethod
    def _unwrap_rows(raw: Any) -> list[Any]:
        data = raw.get("data", raw) if isinstance(raw, dict) else raw
        if isinstance(data, dict):
            for key in ("data", "rows", "list", "items"):
                if isinstance(data.get(key), list):
                    return data[key]
            return []
        return data if isinstance(data, list) else []

    async def instruments(self, query: str = "", limit: int = 100) -> list[dict[str, Any]]:
        now = time.monotonic()
        cached = self._instrument_cache
        if cached is None or now - cached[0] >= self.instruments_ttl:
            async with self._instrument_lock:
                cached = self._instrument_cache
                if cached is None or now - cached[0] >= self.instruments_ttl:
                    raw = await self.client.instruments()
                    parsed: list[MarketInstrument] = []
                    for item in self._unwrap_rows(raw):
                        if not isinstance(item, dict):
                            continue
                        status = str(item.get("status") or "").lower()
                        if status != "online":
                            continue
                        base = str(item.get("base") or item.get("name") or "").strip().upper()
                        quote = str(item.get("quote") or "USDT").strip().upper()
                        if not base or not quote:
                            continue
                        symbol = canonical_symbol(f"{base}_{quote}" if quote != "USDT" else f"{base}USDT")
                        try:
                            precision = int(item.get("pricePrecision", item.get("price_precision", 6)))
                        except (TypeError, ValueError):
                            precision = 6
                        parsed.append(MarketInstrument(
                            symbol=symbol,
                            display=display_symbol(symbol),
                            base=base,
                            quote=quote,
                            pair_code=base if quote == "USDT" else f"{base}_{quote}",
                            price_precision=max(0, min(12, precision)),
                            status=status,
                            icon_url=str(item.get("iconUrl")) if item.get("iconUrl") else None,
                        ))
                    # De-duplicate without trusting exchange ordering.
                    unique = {item.symbol: item for item in parsed}
                    self._instrument_cache = (time.monotonic(), sorted(unique.values(), key=lambda x: (x.quote != "USDT", x.base)))
                    cached = self._instrument_cache

        items = list(cached[1] if cached else [])
        needle = "".join(ch for ch in str(query or "").upper() if ch.isalnum())
        if needle:
            items = [item for item in items if needle in "".join(ch for ch in (item.base + item.quote + item.display).upper() if ch.isalnum())]
        return [item.as_dict() for item in items[: max(1, min(int(limit), 500))]]

    async def instrument(self, symbol: str) -> dict[str, Any] | None:
        target = canonical_symbol(symbol)
        # Search the complete cached exchange catalog before applying the
        # response limit. This keeps direct lookup working even if CoinW
        # lists more than 500 futures instruments.
        for item in await self.instruments(target, 500):
            if item["symbol"] == target:
                return item
        return None

    async def candles(self, symbol: str, timeframe: str = "5m", limit: int = 400) -> list[dict[str, Any]]:
        timeframe = str(timeframe or "").lower()
        if timeframe not in CHART_TIMEFRAMES:
            raise ValueError("unsupported_chart_timeframe")
        canonical = canonical_symbol(symbol)
        instrument = await self.instrument(canonical)
        if instrument is None:
            raise ValueError("market_symbol_not_found")
        raw = await self.client.klines(instrument["pair_code"], timeframe, max(50, min(int(limit), 1000)))
        dedup: dict[int, dict[str, Any]] = {}
        for row in self._unwrap_rows(raw):
            candle = normalize_rest_candle(row)
            if candle:
                dedup[int(candle["timestamp"])] = candle
        return [dedup[key] for key in sorted(dedup)]


    async def snapshot(self, symbol: str) -> dict[str, Any]:
        """Return a REST bootstrap snapshot for the live market panel.

        The websocket remains the real-time source. This snapshot makes the
        order book/trades/market data useful immediately and acts as a safe
        reconnect fallback without exposing private exchange credentials.
        """
        canonical = canonical_symbol(symbol)
        instrument = await self.instrument(canonical)
        if instrument is None:
            raise ValueError("market_symbol_not_found")
        pair = instrument["pair_code"]
        depth_raw, trades_raw, ticker_raw, funding_raw = await asyncio.gather(
            self.client.depth(pair),
            self.client.trades(pair),
            self.client.ticker(pair),
            self.client.funding_rate(pair),
            return_exceptions=True,
        )
        return {
            "symbol": canonical,
            "display": instrument["display"],
            "price_precision": instrument["price_precision"],
            "order_book": normalize_depth(depth_raw) if not isinstance(depth_raw, Exception) else {"asks": [], "bids": [], "timestamp": None},
            "trades": normalize_trades(trades_raw) if not isinstance(trades_raw, Exception) else [],
            "ticker": normalize_ticker(ticker_raw) if not isinstance(ticker_raw, Exception) else {},
            "funding": normalize_funding(funding_raw) if not isinstance(funding_raw, Exception) else {},
            "source": "coinw_futures",
        }
