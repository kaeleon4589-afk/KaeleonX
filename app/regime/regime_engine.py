from app.models.enums import Direction,RegimeState
from app.models.regime import RegimeResult,EngineSignal
class RegimeEngine:
    def evaluate(self,s:EngineSignal,m:EngineSignal,v:EngineSignal,l:EngineSignal,b:EngineSignal):
        if any(x.hard_block for x in (s,m,v,l,b)):
            return RegimeResult(RegimeState.CHOPPY,RegimeState.CHOPPY,Direction.NEUTRAL,'NOISY',0,False,False,None,0,100,0,True,('hard_safety_block',))
        score=.25*s.strength+.20*m.strength+.20*v.strength+.20*l.strength+.15*b.strength
        if v.state=='EXTREME': state=RegimeState.EXTREME
        elif s.state=='TRENDING' and s.direction==m.direction: state=RegimeState.TRENDING
        elif s.state=='RANGING': state=RegimeState.RANGING
        elif s.direction!=m.direction and m.strength>60: state=RegimeState.TRANSITION
        else: state=RegimeState.CHOPPY
        direction=s.direction if s.direction==m.direction else Direction.CONFLICTED
        quality='CLEAN' if min(s.confidence,m.confidence,l.confidence)>=65 else 'NOISY'
        breakout=state in (RegimeState.TRENDING,RegimeState.EXTREME) and direction in (Direction.BULLISH,Direction.BEARISH)
        sweep=state in (RegimeState.RANGING,RegimeState.TRANSITION,RegimeState.TRENDING)
        if state==RegimeState.CHOPPY and v.state=='EXTREME': breakout=sweep=False
        pref='BREAKOUT_RETEST' if breakout and state==RegimeState.TRENDING else 'LIQUIDITY_SWEEP' if sweep else None
        mult=1.0 if quality=='CLEAN' else .75
        if state==RegimeState.CHOPPY: mult*=.5
        return RegimeResult(state,state,direction,quality,score,breakout,sweep,pref,mult,max(0,min(100,100-l.strength)),min(100,(s.confidence+m.confidence+l.confidence)/3))
