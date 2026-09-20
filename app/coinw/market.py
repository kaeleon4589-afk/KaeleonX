import httpx
class CoinWMarketClient:
    def __init__(self,base_url='https://api.coinw.com'): self.base_url=base_url.rstrip('/')
    async def depth(self,instrument):
        async with httpx.AsyncClient(timeout=5) as c:
            r=await c.get(self.base_url+'/v1/perpumPublic/depth',params={'instrument':instrument}); r.raise_for_status(); return r.json()
    async def klines(self,instrument,period='5m',size=200):
        async with httpx.AsyncClient(timeout=5) as c:
            r=await c.get(self.base_url+'/v1/perpumPublic/klines',params={'instrument':instrument,'period':period,'size':size}); r.raise_for_status(); return r.json()
