from .enums import Direction
from dataclasses import dataclass
@dataclass(frozen=True)
class Candle:
    timestamp:int; open:float; high:float; low:float; close:float; volume:float
@dataclass(frozen=True)
class MarketSnapshot:
    symbol:str; timeframe:str; candles:list[Candle]; bid:float|None=None; ask:float|None=None; orderbook_valid:bool=True; data_complete:bool=True
    @property
    def last(self): return self.candles[-1]
    @property
    def spread(self): return None if self.bid is None or self.ask is None or self.bid<=0 else (self.ask-self.bid)/self.bid
@dataclass(frozen=True)
class StructuralLevel:
    price:float; lower:float; upper:float; side:Direction; strength:float; level_type:str; timeframe:str; tests:int=0
