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
    revision:int=0
    current_price:float|None=None
    settlement_pending:bool=False
    net_pnl:float|None=None
    leverage:int=10
    protected:bool=True
    initial_stop_price:float|None=None
    structural_target_price:float|None=None
    target_front_run_ratio:float|None=None
    break_even_price:float|None=None
    break_even_activation_ratio:float=0.55
    profit_lock_activation_ratio:float=0.80
    profit_lock_capture_ratio:float=0.35
    profit_lock_price:float|None=None
    management_stage:str='INITIAL'
    best_price:float|None=None
    estimated_exit_fee_rate:float=0.0006
    break_even_buffer_bps:float=3.0
    protection_update_pending:bool=False
    exit_trigger_price:float|None=None
    stop_gap_bps:float|None=None
    exit_quote_delay_ms:int|None=None
    opened_at:int|None=None; closed_at:int|None=None; exit_price:float|None=None; exit_reason:str|None=None
