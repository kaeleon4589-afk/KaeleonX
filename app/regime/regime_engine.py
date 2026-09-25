from __future__ import annotations
from app.models.enums import Direction,RegimeState
from app.models.regime import RegimeResult
from app.regime.advanced import features,classify,TREND,VOLATILE,RANGE,UNKNOWN
from app.regime.state_machine import advance

class RegimeEngine:
    def __init__(self,*args,**kwargs): self._states={}; self.last_metadata={}; self._bar_times={}
    def evaluate_snapshot(self,snapshot):
        f=features(snapshot.candles,getattr(snapshot,'btc_candles',None)); cand,conf,scores=classify(f)
        stamp = snapshot.candles[-1].timestamp if snapshot.candles else None
        if snapshot.symbol not in self._states or self._bar_times.get(snapshot.symbol) != stamp:
            self._states[snapshot.symbol] = advance(cand, self._states.get(snapshot.symbol))
            self._bar_times[snapshot.symbol] = stamp
        state = self._states[snapshot.symbol]; active=state['active']
        bias=f.get('trend_bias','neutral'); direction=Direction.BULLISH if bias=='long' else (Direction.BEARISH if bias=='short' else Direction.NEUTRAL)
        hard=active==UNKNOWN; breakout=active==TREND and not hard; sweep=active==VOLATILE and not hard
        # Source enforced router keeps RANGE shadow-only: no executable sweep.
        if active==RANGE: sweep=False
        rs=RegimeState.TRENDING if active==TREND else (RegimeState.RANGING if active==RANGE else (RegimeState.EXTREME if active==VOLATILE else RegimeState.TRANSITION))
        risk=1.0 if active==TREND else (.80 if active==VOLATILE else (.65 if active==RANGE else .0))
        self.last_metadata={'candidate':cand,'active':active,'confidence':conf,'scores':scores,'features':f,'state':state}
        return RegimeResult(rs,rs,direction,'HIGH' if conf>=.7 else 'MEDIUM',conf*100,breakout,sweep,'BREAKOUT_RETEST' if active==TREND else ('LIQUIDITY_SWEEP' if sweep else None),risk,f.get('breakout_failure_ratio',0)*100,conf*100,hard,(f'regime={active}',f'candidate={cand}',f'confidence={conf:.2f}'))
    def evaluate(self,*signals):
        # Legacy fallback used by older tests/callers.
        if len(signals)==1 and hasattr(signals[0],'candles'): return self.evaluate_snapshot(signals[0])
        direction=Direction.NEUTRAL
        return RegimeResult(RegimeState.TRANSITION,RegimeState.TRANSITION,direction,'LOW',0,False,False,None,0,0,0,True,('legacy_signals_unsupported',))
