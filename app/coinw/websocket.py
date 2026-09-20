import asyncio, json, websockets
class CoinWWebSocket:
    def __init__(self,url='wss://ws.futurescw.com/perpum',audit=None): self.url=url; self.audit=audit
    async def stream(self,symbols,types=('depth','fills')):
        while True:
            try:
                async with websockets.connect(self.url,ping_interval=20,ping_timeout=10) as ws:
                    for typ in types:
                        for symbol in symbols:
                            await ws.send(json.dumps({'event':'sub','params':{'biz':'futures','pairCode':symbol,'type':typ}}))
                    async for raw in ws:
                        yield json.loads(raw)
            except Exception as exc:
                if self.audit: self.audit.event('WS_RECONNECT',error=str(exc))
                await asyncio.sleep(2)
