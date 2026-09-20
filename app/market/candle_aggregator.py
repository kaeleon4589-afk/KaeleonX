from collections import defaultdict
from app.models.market import Candle

TF_MS={'1m':60000,'3m':180000,'5m':300000,'15m':900000,'30m':1800000,'1h':3600000,'4h':14400000}
class CandleAggregator:
    def __init__(self,timeframes=('1m','5m','15m','1h')): self.timeframes=timeframes; self.current={}; self.closed=defaultdict(list)
    def on_trade(self,symbol,price,quantity,timestamp_ms):
        out=[]
        for tf in self.timeframes:
            span=TF_MS[tf]; start=(timestamp_ms//span)*span; key=(symbol,tf); c=self.current.get(key)
            if c is None or c.timestamp!=start:
                if c is not None: self.closed[key].append(c); out.append((symbol,tf,c))
                c=Candle(start,price,price,price,price,quantity); self.current[key]=c
            else:
                c=Candle(c.timestamp,c.open,max(c.high,price),min(c.low,price),price,c.volume+quantity); self.current[key]=c
        return out
    def history(self,symbol,tf,limit=200): return self.closed[(symbol,tf)][-limit:]
