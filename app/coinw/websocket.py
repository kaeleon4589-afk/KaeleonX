from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass
from typing import Any, Iterable

import websockets


@dataclass(frozen=True)
class DepthQuote:
    symbol: str
    bid: float
    ask: float
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    received_ms: int
    exchange_ts_ms: int | None = None


@dataclass(frozen=True)
class TradeTick:
    symbol: str
    price: float
    quantity: float
    direction: str | None
    received_ms: int
    exchange_ts_ms: int | None = None
    trade_id: str | None = None


class CoinWWebSocket:
    """Small public CoinW websocket adapter used by backend market consumers.

    The frontend already consumes CoinW's public websocket.  This backend
    adapter intentionally keeps parsing conservative: only valid two-sided
    depth books produce executable quotes.  REST remains the safety fallback.
    """

    def __init__(self, url: str = "wss://ws.futurescw.com/perpum", audit=None):
        self.url = url
        self.audit = audit

    @staticmethod
    def _base(symbol: str) -> str:
        value = str(symbol or "").upper().replace("-", "").replace("_", "")
        return value[:-4] if value.endswith("USDT") else value

    @staticmethod
    def _unwrap(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return value

    @classmethod
    def _parse_side(cls, rows: Any, *, reverse: bool) -> list[tuple[float, float]]:
        if not isinstance(rows, list):
            return []
        levels: list[tuple[float, float]] = []
        for row in rows:
            try:
                if isinstance(row, dict):
                    price = row.get("p", row.get("price"))
                    quantity = row.get("m", row.get("quantity", row.get("qty")))
                else:
                    price, quantity = row[:2]
                p = float(price)
                q = float(quantity)
                if math.isfinite(p) and math.isfinite(q) and p > 0 and q > 0:
                    levels.append((p, q))
            except (TypeError, ValueError, IndexError):
                continue
        levels.sort(key=lambda item: item[0], reverse=reverse)
        return levels

    @classmethod
    def parse_depth_quote(cls, payload: Any, *, symbol_map: dict[str, str] | None = None,
                          received_ms: int | None = None) -> DepthQuote | None:
        """Parse a CoinW `depth` websocket message into an executable quote."""
        message = cls._unwrap(payload)
        if not isinstance(message, dict) or str(message.get("type") or "") != "depth":
            return None
        data = cls._unwrap(message.get("data"))
        if not isinstance(data, dict):
            return None

        pair = str(message.get("pairCode") or data.get("pairCode") or data.get("base") or "")
        base = cls._base(pair)
        if not base:
            return None
        mapping = symbol_map or {}
        symbol = mapping.get(base, pair or base)

        bids = cls._parse_side(data.get("bids", data.get("bid")), reverse=True)
        asks = cls._parse_side(data.get("ask", data.get("asks")), reverse=False)
        if not bids or not asks:
            return None
        bid, ask = float(bids[0][0]), float(asks[0][0])
        if not (math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask):
            return None

        now_ms = int(received_ms if received_ms is not None else time.time() * 1000)
        exchange_ts = data.get("ts", data.get("timestamp", message.get("ts")))
        try:
            exchange_ts_ms = int(exchange_ts) if exchange_ts is not None else None
            if exchange_ts_ms is not None and exchange_ts_ms < 100_000_000_000:
                exchange_ts_ms *= 1000
        except (TypeError, ValueError):
            exchange_ts_ms = None
        return DepthQuote(
            symbol=str(symbol), bid=bid, ask=ask, bids=bids, asks=asks,
            received_ms=now_ms, exchange_ts_ms=exchange_ts_ms,
        )


    @classmethod
    def parse_trade_ticks(cls, payload: Any, *, symbol_map: dict[str, str] | None = None,
                          received_ms: int | None = None) -> list[TradeTick]:
        """Parse CoinW futures ``fills`` messages into last-trade trigger ticks.

        Trade ticks are used only as TP/SL trigger evidence.  The simulated fill
        still uses the latest executable bid/ask whenever one is available.
        """
        message = cls._unwrap(payload)
        if not isinstance(message, dict) or str(message.get("type") or "") != "fills":
            return []
        data = cls._unwrap(message.get("data"))
        if not isinstance(data, list):
            return []

        pair = str(message.get("pairCode") or "")
        base = cls._base(pair)
        if not base:
            return []
        mapping = symbol_map or {}
        symbol = mapping.get(base, pair or base)
        now_ms = int(received_ms if received_ms is not None else time.time() * 1000)
        ticks: list[TradeTick] = []
        for row in data:
            if not isinstance(row, dict):
                continue
            try:
                price = float(row.get("price"))
                quantity = float(row.get("quantity", 0) or 0)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(price) or price <= 0:
                continue
            if not math.isfinite(quantity) or quantity < 0:
                quantity = 0.0
            exchange_ts = row.get("createdDate", row.get("ts", message.get("ts")))
            try:
                exchange_ts_ms = int(exchange_ts) if exchange_ts is not None else None
                if exchange_ts_ms is not None and exchange_ts_ms < 100_000_000_000:
                    exchange_ts_ms *= 1000
            except (TypeError, ValueError):
                exchange_ts_ms = None
            trade_id = row.get("id")
            ticks.append(TradeTick(
                symbol=str(symbol), price=price, quantity=quantity,
                direction=(str(row.get("direction")) if row.get("direction") is not None else None),
                received_ms=now_ms, exchange_ts_ms=exchange_ts_ms,
                trade_id=(str(trade_id) if trade_id is not None else None),
            ))
        return ticks

    async def connect(self):
        return await websockets.connect(self.url, ping_interval=20, ping_timeout=10)

    async def subscribe(self, ws, symbols: Iterable[str], types: Iterable[str] = ("depth",)) -> None:
        for typ in types:
            for symbol in symbols:
                await ws.send(json.dumps({
                    "event": "sub",
                    "params": {"biz": "futures", "pairCode": self._base(symbol), "type": typ},
                }))

    async def stream(self, symbols, types=("depth", "fills")):
        """Legacy reconnecting raw-message stream kept for existing callers."""
        while True:
            try:
                ws = await self.connect()
                async with ws:
                    await self.subscribe(ws, symbols, types)
                    async for raw in ws:
                        yield self._unwrap(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.audit:
                    self.audit.event("WS_RECONNECT", "system", error=str(exc))
                await asyncio.sleep(2)
