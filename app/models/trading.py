from dataclasses import dataclass,field
from .enums import Direction,Strategy

@dataclass(frozen=True)
class TradeIntent:
    decision_id:str; symbol:str; strategy:Strategy; direction:Direction
    entry_price:float; stop_price:float; target_price:float; quality:float
    risk_multiplier:float; timeframe:str; reasons:tuple[str,...]=(); metadata:dict=field(default_factory=dict)

@dataclass
class Position:
    position_id:str; decision_id:str; symbol:str; direction:Direction; quantity:float
    entry_price:float; stop_price:float; target_price:float
    status:str='OPEN'; realized_pnl:float=0.0; unrealized_pnl:float=0.0
    tp1_price:float|None=None; tp2_price:float|None=None; remaining_quantity:float|None=None
    tp1_hit:bool=False; stop_moved_to_breakeven:bool=False
    entry_fee:float=0.0; exit_fee:float=0.0; funding_pnl:float=0.0
    opened_at:int|None=None; closed_at:int|None=None; exit_price:float|None=None; exit_reason:str|None=None
