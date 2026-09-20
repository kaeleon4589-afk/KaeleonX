from dataclasses import dataclass,field
from .enums import Direction,RegimeState
@dataclass(frozen=True)
class EngineSignal:
    direction:Direction; strength:float; confidence:float; state:str=''; hard_block:bool=False; metrics:dict=field(default_factory=dict)
@dataclass(frozen=True)
class RegimeResult:
    global_state:RegimeState; asset_state:RegimeState; direction:Direction; quality:str; core_score:float; breakout_allowed:bool; sweep_allowed:bool; preferred:str|None; risk_multiplier:float; trap_risk:float; confidence:float; hard_block:bool=False; reasons:tuple[str,...]=()
