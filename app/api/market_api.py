from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query

from app.api.user_api import current
from app.config.settings import get_settings
from app.coinw.market import CoinWMarketClient
from app.market.chart_service import CHART_TIMEFRAMES, ChartMarketService, canonical_symbol

router = APIRouter(prefix="/market", tags=["market"])
_service: ChartMarketService | None = None


def service() -> ChartMarketService:
    global _service
    if _service is None:
        settings = get_settings()
        _service = ChartMarketService(CoinWMarketClient(base_url=settings.coinw_rest_base_url))
    return _service


def authenticated(authorization: str | None):
    try:
        return current(authorization)
    except HTTPException:
        raise


@router.get("/instruments")
async def instruments(
    q: str = Query(default="", max_length=40),
    limit: int = Query(default=100, ge=1, le=500),
    authorization: str | None = Header(default=None),
):
    authenticated(authorization)
    try:
        items = await service().instruments(q, limit)
        return {"items": items, "count": len(items), "source": "coinw_futures"}
    except Exception as exc:
        raise HTTPException(502, f"coinw_market_instruments_failed:{exc}") from exc


@router.get("/candles")
async def candles(
    symbol: str = Query(min_length=2, max_length=32),
    timeframe: str = Query(default="5m", max_length=4),
    limit: int = Query(default=400, ge=50, le=1000),
    authorization: str | None = Header(default=None),
):
    authenticated(authorization)
    tf = timeframe.lower()
    if tf not in CHART_TIMEFRAMES:
        raise HTTPException(400, "unsupported_chart_timeframe")
    try:
        canonical = canonical_symbol(symbol)
        instrument = await service().instrument(canonical)
        if instrument is None:
            raise HTTPException(404, "market_symbol_not_found")
        items = await service().candles(canonical, tf, limit)
        return {
            "symbol": canonical,
            "display": instrument["display"],
            "timeframe": tf,
            "price_precision": instrument["price_precision"],
            "items": items,
            "count": len(items),
            "source": "coinw_futures",
        }
    except HTTPException:
        raise
    except ValueError as exc:
        code = str(exc)
        status = 404 if code == "market_symbol_not_found" else 400
        raise HTTPException(status, code) from exc
    except Exception as exc:
        raise HTTPException(502, f"coinw_market_candles_failed:{exc}") from exc



@router.get("/snapshot")
async def snapshot(
    symbol: str = Query(min_length=2, max_length=32),
    authorization: str | None = Header(default=None),
):
    authenticated(authorization)
    try:
        canonical = canonical_symbol(symbol)
        payload = await service().snapshot(canonical)
        return payload
    except ValueError as exc:
        code = str(exc)
        status = 404 if code == "market_symbol_not_found" else 400
        raise HTTPException(status, code) from exc
    except Exception as exc:
        raise HTTPException(502, f"coinw_market_snapshot_failed:{exc}") from exc
