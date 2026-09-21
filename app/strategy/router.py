from app.strategy.breakout_retest import BreakoutRetestStrategy
from app.strategy.liquidity_sweep import LiquiditySweepStrategy
from app.models.enums import RegimeState

class StrategyRouter:
    def __init__(self):
        self.breakout=BreakoutRetestStrategy(); self.sweep=LiquiditySweepStrategy(); self.last_trace={}
    def evaluate(self,regime,candles,decision_id,symbol,timeframe,current_price=None,snapshot=None):
        if regime.hard_block:
            self.last_trace={'selected':None,'reason':'regime_hard_block','breakout':{'accepted':False,'reason':'not_run'},'sweep':{'accepted':False,'reason':'not_run'}}
            return None
        candidates=[]; traces={}
        if regime.breakout_allowed:
            x=self.breakout.evaluate(regime,candles,decision_id,symbol,timeframe,current_price,snapshot=snapshot); traces['breakout']=dict(self.breakout.last_trace)
            if x:candidates.append(x)
        else: traces['breakout']={'accepted':False,'reason':'regime_breakout_not_allowed'}
        if regime.sweep_allowed or regime.global_state==RegimeState.TRENDING:
            proxy=regime
            if not regime.sweep_allowed and regime.global_state==RegimeState.TRENDING:
                from dataclasses import replace
                proxy=replace(regime,sweep_allowed=True)
            x=self.sweep.evaluate(proxy,candles,decision_id,symbol,timeframe,current_price,snapshot=snapshot); traces['sweep']=dict(self.sweep.last_trace)
            if x and (regime.sweep_allowed or x.quality>=78):candidates.append(x)
            elif x and not regime.sweep_allowed and x.quality<78: traces['sweep']={'accepted':False,'reason':'trend_probe_quality_below_78','score':x.quality}
        else: traces['sweep']={'accepted':False,'reason':'regime_sweep_not_allowed'}
        selected=max(candidates,key=lambda x:x.quality) if candidates else None
        self.last_trace={'selected':getattr(getattr(selected,'strategy',None),'value',None) if selected else None,'reason':'best_quality' if selected else 'no_valid_setup',**traces}
        return selected
