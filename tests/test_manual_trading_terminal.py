import asyncio
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.coinw.executor import CoinWExecutor
from app.config.settings import Settings
from app.models.enums import Direction
from app.security.credential_vault import CredentialVault
from app.storage.database import Database
from app.trading.manual import ManualTradingService, build_demo_limit_fill
from app.trading.profile import UserTradingProfileService


class BillingStub:
    def entitlement(self, _user_id):
        return SimpleNamespace(live_allowed=True)


class MarketStub:
    def __init__(self, *, bid=99.9, ask=100.0, last=100.0):
        self.bid = bid
        self.ask = ask
        self.last = last

    async def snapshot(self, _symbol):
        return {
            "ticker": {"last": self.last},
            "order_book": {
                "bids": [{"price": self.bid, "quantity": 20}],
                "asks": [{"price": self.ask, "quantity": 20}],
            },
        }


def make_service():
    db = Database()
    settings = Settings(
        environment="test",
        credential_encryption_key=Fernet.generate_key().decode(),
        paper_initial_equity=100,
        paper_slippage_bps=0,
        paper_taker_fee=0.0006,
        manual_trading_enabled=True,
        manual_trading_min_margin=1,
        manual_trading_max_leverage=50,
    )
    profiles = UserTradingProfileService(
        db,
        CredentialVault(settings.credential_encryption_key),
        initial_demo_equity=100,
    )
    profiles.save("u", execution_mode="demo", trading_enabled=False)
    service = ManualTradingService(settings, db, profiles, BillingStub())
    service.market = MarketStub()
    return service, db, profiles, settings


def order_request(**overrides):
    data = dict(
        client_order_id="MANUAL-test-000001",
        mode="demo",
        symbol="BTCUSDT",
        side="LONG",
        order_type="MARKET",
        margin=10.0,
        leverage=10,
        limit_price=None,
        stop_loss=99.0,
        take_profit=101.5,
        confirm_live=False,
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def test_demo_manual_market_is_persisted_and_idempotent():
    async def scenario():
        service, db, _, _ = make_service()
        request = order_request()
        first = await service.place_order("u", request)
        second = await service.place_order("u", request)

        assert first["filled"] is True
        assert second["idempotent_replay"] is True
        assert db.count("manual_orders", {"user_id": "u"}) == 1
        rows = db.find_many("positions", {"user_id": "u", "mode": "demo"}, limit=0)
        assert len(rows) == 1
        position = rows[0]
        assert position["source"] == "MANUAL"
        assert position["order_type"] == "MARKET"
        assert position["position_margin"] == pytest.approx(10.0)
        assert position["leverage"] == 10
        assert position["stop_price"] == pytest.approx(99.0)
        assert position["target_price"] == pytest.approx(101.5)

        state = await service.state("u")
        assert state["account"]["used_margin"] == pytest.approx(10.0)
        assert state["account"]["available"] < 90.0  # entry fee is booked immediately
        assert len(state["positions"]) == 1

    asyncio.run(scenario())


def test_demo_limit_reserves_margin_and_builds_fill_without_browser_state():
    async def scenario():
        service, db, _, settings = make_service()
        request = order_request(
            client_order_id="MANUAL-limit-000001",
            order_type="LIMIT",
            limit_price=99.5,
            stop_loss=98.5,
            take_profit=101.0,
        )
        result = await service.place_order("u", request)
        assert result["accepted"] is True and result["filled"] is False
        state = await service.state("u")
        assert state["account"]["frozen_margin"] == pytest.approx(10.0)
        assert state["account"]["available"] == pytest.approx(90.0)

        stored = db.find_one("manual_orders", {"manual_order_id": result["manual_order_id"]})
        position = build_demo_limit_fill(stored, 99.4, settings)
        assert position.source == "MANUAL"
        assert position.order_type == "LIMIT"
        assert position.position_margin == pytest.approx(10.0)
        assert position.entry_price == pytest.approx(99.4)
        assert position.quantity == pytest.approx(100.0 / 99.4)

    asyncio.run(scenario())


def test_demo_manual_close_books_realized_pnl_and_fees():
    async def scenario():
        service, db, profiles, _ = make_service()
        opened = await service.place_order("u", order_request())
        position_id = opened["position_id"]
        service.market = MarketStub(bid=101.0, ask=101.1, last=101.05)

        before = profiles.demo_account.balance("u")
        result = await service.close_position("u", position_id, confirm_live=False)
        after = profiles.demo_account.balance("u")

        assert result["pending"] is False
        closed = db.find_one("positions", {"position_id": position_id})
        assert closed["status"] == "CLOSED"
        assert closed["exit_reason"] == "MANUAL_CLOSE"
        assert closed["exit_price"] == pytest.approx(101.0)
        assert closed["net_pnl"] > 0
        assert after > before

        state = await service.state("u")
        assert len(state["position_history"]) == 1
        assert state["position_history"][0]["position_id"] == position_id
        assert state["position_history"][0]["source"] == "MANUAL"

    asyncio.run(scenario())


def test_manual_geometry_rejects_invalid_long():
    async def scenario():
        service, _, _, _ = make_service()
        with pytest.raises(ValueError, match="invalid_manual_long_geometry"):
            await service.place_order("u", order_request(stop_loss=100.5, take_profit=101.5))
    asyncio.run(scenario())


class FakeClient:
    async def request(self, method, path, params=None, private=True):
        assert method == "GET"
        assert path == "/v1/perpum/instruments"
        return {"data": [{"status": "online", "maxLeverage": 50, "pricePrecision": 2, "oneLotSize": 0.001, "minSize": 1}]}


class FakeOrders:
    def __init__(self):
        self.client = FakeClient()
        self.payload = None

    async def place(self, payload):
        self.payload = payload
        return {"data": {"value": "coinw-order-1"}}


class FakePositions:
    pass


def test_live_limit_payload_uses_quote_notional_and_does_not_fake_fill():
    async def scenario():
        orders = FakeOrders()
        executor = CoinWExecutor(orders, FakePositions(), enabled=True)
        result = await executor.submit_manual(
            client_order_id="MANUAL-live-1",
            symbol="BTCUSDT",
            direction=Direction.LONG,
            margin=10,
            leverage=10,
            order_type="LIMIT",
            stop_loss=98,
            take_profit=104,
            limit_price=100,
            reference_price=100,
        )
        assert result["accepted"] is True
        assert result["filled"] is False
        assert result["status"] == "OPEN"
        assert result["order_id"] == "coinw-order-1"
        assert orders.payload["positionType"] == "plan"
        assert orders.payload["quantityUnit"] == 0
        assert orders.payload["quantity"] == "100.0"
        assert orders.payload["thirdOrderId"] == "MANUAL-live-1"
        assert orders.payload["stopLossPrice"] == pytest.approx(98.0)
        assert orders.payload["stopProfitPrice"] == pytest.approx(104.0)
    asyncio.run(scenario())


class HistoricalOrders(FakeOrders):
    def __init__(self):
        super().__init__()
        self.tpsl = None

    async def information(self, source_ids, position_type="execute"):
        # Filled MARKET orders may already be absent from the current-order API.
        return {"data": []}

    async def history(self, instrument, position_type="execute", page=1, page_size=50):
        return {
            "data": {
                "rows": [{
                    "id": "coinw-order-2",
                    "openId": "coinw-position-exact",
                    "thirdOrderId": "MANUAL-live-market-1",
                    "orderStatus": "finish",
                    "direction": "long",
                    "avgPrice": "100.0",
                    "quantity": "100",
                    "quantityUnit": 0,
                    "leverage": 10,
                    "margin": "10",
                    "positionMargin": "10",
                    "positionModel": 0,
                    "status": "open",
                }]
            }
        }

    async def set_tpsl(self, position_id, instrument, stop_loss=None, take_profit=None):
        self.tpsl = (position_id, instrument, stop_loss, take_profit)
        return {"code": 0}


class HistoricalPositions:
    def __init__(self):
        self.requested_open_ids = None

    async def current(self, instrument, open_ids=None):
        self.requested_open_ids = list(open_ids or [])
        return {
            "data": [{
                "id": "coinw-position-exact",
                "instrument": "BTC",
                "status": "open",
                "direction": "long",
                "openPrice": "100.0",
                "quantity": "100",
                "quantityUnit": 0,
                "leverage": 10,
                "positionMargin": "10",
                "margin": "10",
                "positionModel": 0,
                "profitUnreal": "0.2",
                "liquidationPrice": "91.0",
                "stopLossPrice": "98.0",
                "stopProfitPrice": "104.0",
            }]
        }


def test_live_market_confirmation_uses_history_open_id_instead_of_guessing_position():
    async def scenario():
        orders = HistoricalOrders()
        positions = HistoricalPositions()
        executor = CoinWExecutor(orders, positions, enabled=True, confirm_attempts=1, confirm_delay=0)
        # Override the place response for this MARKET order.
        async def place(payload):
            orders.payload = payload
            return {"data": {"value": "coinw-order-2"}}
        orders.place = place

        result = await executor.submit_manual(
            client_order_id="MANUAL-live-market-1",
            symbol="BTCUSDT",
            direction=Direction.LONG,
            margin=10,
            leverage=10,
            order_type="MARKET",
            stop_loss=98,
            take_profit=104,
            reference_price=100,
        )

        assert result["filled"] is True
        assert result["position_id"] == "coinw-position-exact"
        assert positions.requested_open_ids == ["coinw-position-exact"]
        assert result["position"].source == "MANUAL"
        assert result["position"].position_margin == pytest.approx(10.0)
        assert orders.tpsl[0] == "coinw-position-exact"

    asyncio.run(scenario())


class RecoveryAccount:
    async def assets(self, _coin="usdt"):
        return {"data": {"availableUsdt": "90", "alMargin": "0", "alFreeze": "10", "availableMargin": "90"}}


class RecoveryOrders:
    def __init__(self):
        self.open_calls = 0
        self.history_calls = 0

    async def open_orders(self, instrument, position_type="execute"):
        self.open_calls += 1
        assert instrument == "BTC"
        assert position_type == "plan"
        return {
            "data": [{
                "id": "coinw-recovered-order",
                "thirdOrderId": "MANUAL-unknown-1",
                "orderStatus": "unFinish",
                "direction": "long",
            }]
        }

    async def history(self, instrument, position_type="execute", page=1, page_size=50):
        self.history_calls += 1
        return {"data": {"rows": []}}


class RecoveryAdapter:
    def __init__(self):
        self.account = RecoveryAccount()
        self.orders = RecoveryOrders()

    @staticmethod
    def _instrument(symbol):
        return str(symbol).upper().replace("USDT", "")

    async def current_position_rows(self, _symbol, _open_ids=None):
        return []


def test_live_unknown_without_order_id_recovers_by_third_order_id_without_resubmit():
    async def scenario():
        service, db, profiles, _ = make_service()
        profiles.save(
            "u", execution_mode="demo", trading_enabled=False,
            api_key="test-api-key", api_secret="test-api-secret",
        )
        profiles.mark_verified("u", 100)
        profiles.save("u", execution_mode="live", trading_enabled=False)

        db.upsert("manual_orders", {"manual_order_id": "MANUAL-local-unknown"}, {
            "manual_order_id": "MANUAL-local-unknown",
            "client_order_id": "MANUAL-unknown-1",
            "user_id": "u",
            "mode": "live",
            "source": "MANUAL",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "direction": "LONG",
            "order_type": "LIMIT",
            "margin": 10.0,
            "leverage": 10,
            "notional": 100.0,
            "limit_price": 100.0,
            "stop_price": 98.0,
            "target_price": 103.0,
            "status": "UNKNOWN",
            "accepted": False,
            "filled": False,
            "reconcile_required": True,
            "created_at": 1,
        })

        adapter = RecoveryAdapter()
        async def fake_adapter(_user_id):
            return adapter
        service._live_adapter = fake_adapter

        state = await service.state("u")
        recovered = db.find_one("manual_orders", {"manual_order_id": "MANUAL-local-unknown"})
        assert recovered["order_id"] == "coinw-recovered-order"
        assert recovered["status"] == "OPEN"
        assert recovered["reconcile_required"] is False
        assert adapter.orders.open_calls == 1
        assert adapter.orders.history_calls == 0
        assert len(state["open_orders"]) == 1
        assert state["open_orders"][0]["order_id"] == "coinw-recovered-order"
        assert state["open_orders"][0]["status"] == "OPEN"

    asyncio.run(scenario())


def test_demo_manual_market_allows_open_without_tp_sl():
    async def scenario():
        service, db, _, _ = make_service()
        request = order_request(
            client_order_id="MANUAL-no-protection-1",
            stop_loss=None,
            take_profit=None,
        )
        result = await service.place_order("u", request)
        assert result["filled"] is True
        position = db.find_one("positions", {"position_id": result["position_id"]})
        assert position["stop_price"] == pytest.approx(0.0)
        assert position["target_price"] == pytest.approx(0.0)
    asyncio.run(scenario())


def test_demo_marketable_limit_fills_immediately_instead_of_staying_pending():
    async def scenario():
        service, db, _, _ = make_service()
        # Ask is 100. A LONG limit at 100.5 is already marketable and must fill
        # at the best ask rather than being left indefinitely in OPEN state.
        result = await service.place_order("u", order_request(
            client_order_id="MANUAL-marketable-limit-1",
            order_type="LIMIT",
            limit_price=100.5,
            stop_loss=None,
            take_profit=None,
        ))
        assert result["filled"] is True
        assert result["status"] == "FILLED"
        assert result["fill_price"] == pytest.approx(100.0)
        stored = db.find_one("manual_orders", {"manual_order_id": result["manual_order_id"]})
        assert stored["status"] == "FILLED"
    asyncio.run(scenario())


def test_monitor_only_snapshot_keeps_owner_of_pending_manual_limit():
    from app.trading.runtime import UserTradingRuntimeManager

    manager = object.__new__(UserTradingRuntimeManager)
    manager._manual_order_users_by_symbol = {"ADAUSDT": {"u"}}
    runtime = SimpleNamespace(
        user_id="u",
        position_manager=SimpleNamespace(positions={}),
        orchestrator=SimpleNamespace(pending_execution=None),
    )
    assert manager._runtime_tracks_monitor_symbol(runtime, "ADAUSDT") is True
    assert manager._runtime_tracks_monitor_symbol(runtime, "BTCUSDT") is False
