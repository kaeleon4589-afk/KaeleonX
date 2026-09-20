from dataclasses import dataclass,field
import time
@dataclass
class LiveMarketState:
    symbol:str; bid:float=0; ask:float=0; last:float=0; orderbook_valid:bool=True; last_update_ms:int=0
    bids:list=field(default_factory=list); asks:list=field(default_factory=list)
    def update_book(self,bids,asks,ts):
        self.bids=bids; self.asks=asks; self.bid=float(bids[0][0]) if bids else 0; self.ask=float(asks[0][0]) if asks else 0; self.last_update_ms=ts; self.orderbook_valid=self.bid>0 and self.ask>=self.bid
    @property
    def spread_bps(self): return ((self.ask-self.bid)/self.bid*10000) if self.bid>0 and self.ask>0 else float('inf')
    def fresh(self,max_age_ms=5000): return self.last_update_ms and int(time.time()*1000)-self.last_update_ms<=max_age_ms
