from dataclasses import dataclass
from app.models.enums import Direction, Strategy

@dataclass(frozen=True)
class ExitPlan:
    stop_price: float
    tp1_price: float
    tp2_price: float
    risk_distance: float
    rr1: float
    rr2: float

class TpSlEngine:
    """Central TP/SL rules. No fixed percentage stops.

    SL is structural: sweep extreme or breakout/retest invalidation zone plus ATR buffer.
    TP1/TP2 are R-multiples, then constrained by nearby market/liquidity barriers when supplied.
    """
    def __init__(self, rr1=1.0, rr2=2.0, buffer_atr=.12):
        self.rr1=rr1; self.rr2=rr2; self.buffer_atr=buffer_atr

    def build(self, strategy, direction, entry, structural_stop, atr, barrier_tp1=None, barrier_tp2=None, min_rr2=1.8):
        if atr<=0: raise ValueError('ATR must be positive')
        if direction==Direction.LONG:
            stop=structural_stop-self.buffer_atr*atr
            risk=entry-stop
            if risk<=0: raise ValueError('Invalid long structural stop')
            tp1=entry+risk*self.rr1
            tp2=entry+risk*max(self.rr2,min_rr2)
            if barrier_tp1 and barrier_tp1>entry: tp1=min(tp1,barrier_tp1) if barrier_tp1-entry>=risk else tp1
            if barrier_tp2 and barrier_tp2>entry and barrier_tp2-entry>=risk*min_rr2: tp2=min(tp2,barrier_tp2)
        else:
            stop=structural_stop+self.buffer_atr*atr
            risk=stop-entry
            if risk<=0: raise ValueError('Invalid short structural stop')
            tp1=entry-risk*self.rr1
            tp2=entry-risk*max(self.rr2,min_rr2)
            if barrier_tp1 and barrier_tp1<entry and entry-barrier_tp1>=risk: tp1=max(tp1,barrier_tp1)
            if barrier_tp2 and barrier_tp2<entry and entry-barrier_tp2>=risk*min_rr2: tp2=max(tp2,barrier_tp2)
        if direction==Direction.LONG and not (stop<entry<tp1<=tp2): raise ValueError('Invalid long TP/SL plan')
        if direction==Direction.SHORT and not (tp2<=tp1<entry<stop): raise ValueError('Invalid short TP/SL plan')
        return ExitPlan(stop,tp1,tp2,risk,(tp1-entry)/risk if direction==Direction.LONG else (entry-tp1)/risk,(tp2-entry)/risk if direction==Direction.LONG else (entry-tp2)/risk)
