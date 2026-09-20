class CoinWAccountAPI:
    def __init__(self, client):
        self.client = client

    async def assets(self, quote="usdt"):
        return await self.client.request("GET", "/v1/perpum/account/getUserAssets", {"quote": quote}, private=True)
