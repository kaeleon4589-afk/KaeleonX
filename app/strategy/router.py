from app.strategy.breakout_retest import BreakoutRetestStrategy
from app.strategy.liquidity_sweep import LiquiditySweepStrategy
from app.models.enums import RegimeState
class StrategyRouter:
    def __init__(self): self.breakout=BreakoutRetestStrategy(); self.sweep=LiquiditySweepStrategy()
    def evaluate(self,regime,candles,decision_id,symbol,timeframe,current_price=None,snapshot=None):
        # Trend routes primarily to breakout; volatile/range to sweep. A high-quality
        # sweep can still be probed in trend regimes, matching the source router.
        if regime.hard_block:return None
        candidates=[]
        if regime.breakout_allowed:
            x=self.breakout.evaluate(regime,candles,decision_id,symbol,timeframe,current_price,snapshot=snapshot)
            if x:candidates.append(x)
        if regime.sweep_allowed or regime.global_state==RegimeState.TRENDING:
            # shadow/probe liquidity in trending conditions only when regime permits no hard block
            proxy=regime
            if not regime.sweep_allowed and regime.global_state==RegimeState.TRENDING:
                from dataclasses import replace
                proxy=replace(regime,sweep_allowed=True)
            x=self.sweep.evaluate(proxy,candles,decision_id,symbol,timeframe,current_price,snapshot=snapshot)
            if x and (regime.sweep_allowed or x.quality>=78):candidates.append(x)
        return max(candidates,key=lambda x:x.quality) if candidates else None
