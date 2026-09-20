from app.strategy.breakout_retest import BreakoutRetestStrategy
from app.strategy.liquidity_sweep import LiquiditySweepStrategy

class StrategyRouter:
    def __init__(self, breakout=None, sweep=None):
        self.breakout=breakout or BreakoutRetestStrategy(); self.sweep=sweep or LiquiditySweepStrategy()
    def evaluate(self, regime, candles, decision_id, symbol, timeframe, current_price=None):
        intents=[]
        if regime.breakout_allowed:
            x=self.breakout.evaluate(regime,candles,decision_id,symbol,timeframe,current_price)
            if x: intents.append(x)
        if regime.sweep_allowed:
            x=self.sweep.evaluate(regime,candles,decision_id,symbol,timeframe,current_price)
            if x: intents.append(x)
        return max(intents,key=lambda x:x.quality) if intents else None
