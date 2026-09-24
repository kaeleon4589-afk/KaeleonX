from __future__ import annotations
import httpx

_GRANULARITY={'1m':'0','5m':'1','15m':'2','1h':'3','4h':'4','1d':'5','1w':'6','3m':'7','30m':'8','1M':'9'}

def _base(instrument:str)->str:
    s=str(instrument or '').upper().replace('-','').replace('_','')
    if s.endswith('USDT'): s=s[:-4]
    return s

class CoinWMarketClient:
    def __init__(self,base_url='https://api.coinw.com',timeout=6.0): self.base_url=base_url.rstrip('/'); self.timeout=timeout
    async def _get(self,path,params=None):
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r=await c.get(self.base_url+path,params=params or {}); r.raise_for_status(); payload=r.json()
            if isinstance(payload,dict) and str(payload.get('code','0')) not in {'0','200'}: raise RuntimeError(f"coinw_public_api_error:{payload.get('code')}:{payload.get('msg','')}")
            return payload
    async def depth(self,instrument): return await self._get('/v1/perpumPublic/depth',{'base':_base(instrument)})
    async def klines(self,instrument,period='5m',size=320):
        return await self._get('/v1/perpumPublic/klines',{'currencyCode':_base(instrument),'granularity':_GRANULARITY.get(period,'1'),'klineType':'0','limit':min(max(int(size),1),1500)})
    async def ticker(self,instrument): return await self._get('/v1/perpumPublic/ticker',{'instrument':_base(instrument)})
    async def tickers(self): return await self._get('/v1/perpumPublic/tickers')
    async def instruments(self): return await self._get('/v1/perpum/instruments')
