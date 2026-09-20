from app.models.market import Candle,MarketSnapshot
from app.models.regime import RegimeResult
from app.models.enums import Direction,RegimeState
from app.strategy.router import StrategyRouter
from app.risk.manager import RiskManager
from app.execution.paper import PaperExecutionEngine

def run():
    c=[]; p=100
    for i in range(50):
        c.append(Candle(i,p,p+1,p-1,p+.2,100)); p+=.2
    m=MarketSnapshot('BTC','5m',c,99.9,100.1)
    r=RegimeResult(RegimeState.TRENDING,RegimeState.TRENDING,Direction.BULLISH,'CLEAN',75,True,True,'BREAKOUT_RETEST',1,10,80)
    intent=StrategyRouter().evaluate(r,c,'D1','BTC','5m',m.last.close)
    if intent:
        rd=RiskManager().evaluate(intent,10000)
        if rd.approved: print(PaperExecutionEngine().submit(intent,rd.quantity))
    print('SMOKE_OK')
if __name__=='__main__': run()
