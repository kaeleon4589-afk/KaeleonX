from __future__ import annotations


class CoinWOrdersAPI:
    def __init__(self, client):
        self.client = client

    async def place(self, payload: dict):
        return await self.client.request("POST", "/v1/perpum/order", payload, private=True)

    async def open_orders(self, instrument: str, position_type: str = "execute"):
        return await self.client.request(
            "GET", "/v1/perpum/orders/open",
            {"instrument": instrument, "positionType": position_type},
            private=True,
        )

    async def information(self, source_ids, position_type: str = "execute"):
        ids = ",".join(map(str, source_ids)) if not isinstance(source_ids, str) else source_ids
        return await self.client.request(
            "GET", "/v1/perpum/order",
            {"sourceIds": ids, "positionType": position_type},
            private=True,
        )

    async def history(self, instrument: str, position_type: str = "execute", *, page: int = 1, page_size: int = 50):
        """Executed orders from CoinW's 7-day history.

        CoinW's current-order lookup is documented for unfulfilled orders, so
        an order can disappear from that endpoint exactly when it fills.  The
        historical endpoint carries both ``thirdOrderId`` and ``openId`` and is
        therefore the authoritative bridge from our idempotency key to the
        resulting position.
        """
        params = {
            "instrument": instrument,
            "originType": position_type,
            "page": max(1, int(page)),
            "pageSize": max(1, min(100, int(page_size))),
        }
        return await self.client.request(
            "GET", "/v1/perpum/orders/history", params, private=True
        )

    async def cancel(self, order_ids):
        ids = ",".join(map(str, order_ids)) if not isinstance(order_ids, str) else order_ids
        return await self.client.request(
            "DELETE", "/v1/perpum/order", {"id": ids}, private=True
        )

    async def get_tpsl(self, position_id, instrument: str):
        return await self.client.request(
            "GET", "/v1/perpum/TPSL",
            {"openId": position_id, "stopFrom": 2, "instrument": instrument},
            private=True,
        )

    async def set_tpsl(self, position_id, instrument: str,
                       stop_loss: float | None = None,
                       take_profit: float | None = None):
        payload = {"id": position_id, "instrument": instrument}
        if stop_loss is not None:
            payload["stopLossPrice"] = stop_loss
        if take_profit is not None:
            payload["stopProfitPrice"] = take_profit
        return await self.client.request(
            "POST", "/v1/perpum/TPSL", payload, private=True
        )

    async def add_partial_tpsl(self, position_id, instrument: str,
                               stop_loss: float | None,
                               take_profit: float | None,
                               close_piece):
        payload = {
            "id": position_id,
            "instrument": instrument,
            "priceType": 2,
            "stopFrom": 2,
            "stopType": 1,
            "closePiece": close_piece,
        }
        if stop_loss is not None:
            payload["stopLossPrice"] = stop_loss
        if take_profit is not None:
            payload["stopProfitPrice"] = take_profit
        return await self.client.request(
            "POST", "/v1/perpum/addTpsl", payload, private=True
        )
