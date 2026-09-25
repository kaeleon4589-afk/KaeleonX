import asyncio
from pathlib import Path

from app.market.chart_service import (
    ChartMarketService,
    canonical_symbol,
    normalize_depth,
    normalize_funding,
    normalize_rest_candle,
    normalize_ticker,
    normalize_trades,
)

ROOT = Path(__file__).resolve().parents[1]


class FakeCoinWMarket:
    def __init__(self):
        self.instrument_calls = 0
        self.kline_calls = []

    async def instruments(self):
        self.instrument_calls += 1
        rows = [
            {"base": "btc", "quote": "usdt", "status": "online", "pricePrecision": 1},
            {"base": "ondo", "quote": "usdt", "status": "online", "pricePrecision": 6},
            {"base": "eth", "quote": "usdc", "status": "online", "pricePrecision": 2},
            {"base": "old", "quote": "usdt", "status": "offline", "pricePrecision": 4},
        ]
        # Put the requested test instrument after 500 entries to prove exact
        # lookup is performed against the full catalog before response slicing.
        rows = [
            {"base": f"coin{i}", "quote": "usdt", "status": "online", "pricePrecision": 4}
            for i in range(520)
        ] + rows
        return {"code": 0, "data": rows}

    async def depth(self, instrument):
        return {"code": 0, "data": {"ask": [[100.2, 2.0], [100.1, 1.0]], "bids": [[99.8, 3.0], [99.9, 1.5]], "ts": 1752832800000}}

    async def trades(self, instrument):
        return {"code": 0, "data": [
            {"id": "b", "createdDate": 1752832800100, "price": 100.0, "quantity": 2.0, "piece": 2, "direction": "long"},
            {"id": "a", "createdDate": 1752832799000, "price": 99.9, "quantity": 1.0, "piece": 1, "direction": "short"},
        ]}

    async def ticker(self, instrument):
        return {"code": 0, "data": {"last_price": 100.0, "fair_price": 99.95, "high": 103, "low": 97, "rise_fall_rate": 0.012, "total_volume": 12345}}

    async def funding_rate(self, instrument):
        return {"code": 0, "data": {"value": 0.0001, "ts": 1752832800000}}

    async def klines(self, instrument, period="5m", size=320):
        self.kline_calls.append((instrument, period, size))
        return {
            "code": 0,
            "data": [
                # Current CoinW documented geometry: ts, open, high, low, close, volume.
                [1752832500000, 0.545, 0.550, 0.542, 0.548, 1200.0],
                [1752832800000, 0.548, 0.553, 0.547, 0.551, 900.0],
            ],
        }


def test_symbol_normalization_supports_usdt_and_usdc():
    assert canonical_symbol("ONDO") == "ONDOUSDT"
    assert canonical_symbol("btc/usdt") == "BTCUSDT"
    assert canonical_symbol("eth_usdc") == "ETH_USDC"
    assert canonical_symbol("ethusdc") == "ETH_USDC"


def test_rest_candle_normalizer_accepts_coinw_and_legacy_ordering():
    documented = normalize_rest_candle([1752832500000, 118815.0, 118882.6, 118808.3, 118882.6, 61.333])
    assert documented is not None
    assert documented["open"] == 118815.0
    assert documented["high"] == 118882.6

    legacy = normalize_rest_candle([1752832500000, 10.0, 11.0, 9.0, 10.5, 25.0])
    assert legacy is not None
    assert legacy["open"] == 10.0
    assert legacy["high"] == 11.0


def test_market_search_filters_offline_and_finds_any_catalog_position():
    async def run():
        service = ChartMarketService(FakeCoinWMarket(), instruments_ttl=60)
        ondo = await service.instruments("ondo", 20)
        assert [item["symbol"] for item in ondo] == ["ONDOUSDT"]
        assert ondo[0]["pair_code"] == "ONDO"
        assert ondo[0]["price_precision"] == 6

        eth = await service.instruments("ethusdc", 20)
        assert [item["symbol"] for item in eth] == ["ETH_USDC"]
        assert eth[0]["pair_code"] == "ETH_USDC"

        assert await service.instruments("old", 20) == []
        # COIN519 is beyond the first 500 entries in the fake exchange response.
        exact = await service.instrument("COIN519USDT")
        assert exact is not None
        assert exact["symbol"] == "COIN519USDT"

    asyncio.run(run())


def test_market_candles_validate_timeframe_and_return_sorted_numeric_bars():
    async def run():
        fake = FakeCoinWMarket()
        service = ChartMarketService(fake, instruments_ttl=60)
        candles = await service.candles("ONDOUSDT", "5m", 400)
        assert len(candles) == 2
        assert candles[0]["timestamp"] < candles[1]["timestamp"]
        assert candles[-1]["close"] == 0.551
        assert fake.kline_calls[-1] == ("ONDO", "5m", 400)

        try:
            await service.candles("ONDOUSDT", "2m", 400)
        except ValueError as exc:
            assert str(exc) == "unsupported_chart_timeframe"
        else:
            raise AssertionError("unsupported timeframe must be rejected")

    asyncio.run(run())



def test_market_microstructure_normalizers_and_snapshot():
    depth = normalize_depth({"data": {"ask": [[100.2, 2], [100.1, 1]], "bids": [[99.8, 3], [99.9, 1.5]], "ts": 10}})
    assert [row["price"] for row in depth["asks"]] == [100.1, 100.2]
    assert [row["price"] for row in depth["bids"]] == [99.9, 99.8]

    trades = normalize_trades({"data": [
        {"id": "1", "createdDate": 2, "price": 100, "quantity": 1, "direction": "long"},
        {"id": "2", "createdDate": 3, "price": 101, "quantity": 2, "direction": "short"},
    ]})
    assert [row["id"] for row in trades] == ["2", "1"]
    assert trades[0]["direction"] == "short"

    ticker = normalize_ticker({"data": {"last_price": 100, "fair_price": 99.9, "rise_fall_rate": 0.01, "total_volume": 20}})
    assert ticker["last"] == 100
    assert ticker["index_price"] == 99.9
    assert ticker["change_rate"] == 0.01

    funding = normalize_funding({"data": {"value": 0.0001, "ts": 123}})
    assert funding == {"rate": 0.0001, "timestamp": 123, "kind": "last_settlement"}

    async def run():
        service = ChartMarketService(FakeCoinWMarket(), instruments_ttl=60)
        snapshot = await service.snapshot("ONDOUSDT")
        assert snapshot["symbol"] == "ONDOUSDT"
        assert snapshot["order_book"]["asks"][0]["price"] == 100.1
        assert snapshot["trades"][0]["id"] == "b"
        assert snapshot["ticker"]["last"] == 100.0
        assert snapshot["funding"]["rate"] == 0.0001

    asyncio.run(run())

def test_frontend_chart_contract_contains_live_coinw_features():
    chart = (ROOT / "frontend/src/components/trading/LiveMarketChart.tsx").read_text(encoding="utf-8")
    micro = (ROOT / "frontend/src/components/trading/MarketMicrostructure.tsx").read_text(encoding="utf-8")
    dashboard = (ROOT / "frontend/src/pages/DashboardPage.tsx").read_text(encoding="utf-8")
    api = (ROOT / "frontend/src/lib/api.ts").read_text(encoding="utf-8")
    package = (ROOT / "frontend/package.json").read_text(encoding="utf-8")

    assert "<LiveMarketChart positions={ops.open} closedPositions={ops.closed}/>" in dashboard
    assert "candles_swap_utc" in chart
    assert "mark_price" in chart
    assert "index_price" in chart
    assert "funding_rate" in chart
    assert "depth" in chart
    assert "fills" in chart
    assert "ticker_swap" in chart
    assert "MarketMicrostructure" in chart
    assert "fibonacciLine" in chart
    assert "kaeleonRiskZone" in chart
    assert "kaeleonTradeMarker" in chart
    assert "BE EST." in chart
    assert "priceChannelLine" in chart
    assert "simpleAnnotation" in chart
    for feature in ("Order Book", "Trades", "Market Data", "Depth", "Next funding", "Volumen por zona", "Presión derivada"):
        assert feature in micro
    assert "Buscar cualquier par de futuros en CoinW" in chart
    assert "registerOverlay" in chart
    assert "kaeleonTradeLevel" in chart
    assert "Ver operación" in chart
    assert "manualSelectionRef" in chart
    for timeframe in ("1m", "3m", "5m", "15m", "30m", "1H", "4H", "1D"):
        assert timeframe in chart
    for level in ("ENTRY", "TP1", "TP2", "SL"):
        assert level in chart
    for indicator in ("VOL", "MA", "EMA", "BOLL", "MACD", "RSI"):
        assert indicator in chart
    assert "/market/instruments" in api
    assert "/market/candles" in api
    assert "/market/snapshot" in api
    assert '"klinecharts": "10.0.3"' in package
