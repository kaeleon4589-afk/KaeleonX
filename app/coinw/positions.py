from __future__ import annotations

class CoinWPositionsAPI:
    def __init__(self, client):
        self.client = client

    async def current(self, instrument: str, open_ids=None):
        params = {"instrument": instrument}
        if open_ids:
            params["openIds"] = ",".join(map(str, open_ids)) if not isinstance(open_ids, str) else open_ids
        return await self.client.request("GET", "/v1/perpum/positions", params, private=True)

    async def close(self, position_id, close_rate=None, close_num=None, position_type="execute", order_price=None):
        payload = {"id": position_id, "positionType": position_type}
        if close_rate is not None:
            payload["closeRate"] = str(close_rate)
        if close_num is not None:
            payload["closeNum"] = close_num
        if order_price is not None:
            payload["orderPrice"] = order_price
        return await self.client.request("DELETE", "/v1/perpum/positions", payload, private=True)

    async def close_all_market(self, instrument: str):
        return await self.client.request("DELETE", "/v1/perpum/allpositions", {"instrument": instrument}, private=True)
