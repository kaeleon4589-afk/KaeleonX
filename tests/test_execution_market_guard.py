from types import SimpleNamespace
from app.execution.paper import PaperExecutionEngine
from app.models.enums import Direction, Strategy
from app.models.trading import TradeIntent


def _intent():
    return TradeIntent(
        'd1','ARB',Strategy.LIQUIDITY_SWEEP,Direction.SHORT,
        0.2273,0.2288911,0.2259183,86.1,1.0,'5m',(),{}
    )


def test_paper_rejects_missing_orderbook_without_typeerror():
    engine=PaperExecutionEngine()
    result=engine.submit(_intent(), 10.0, {'bid':None,'ask':None,'last':0.2273,'ts':1})
    assert result == {'accepted':False,'filled':False,'reason':'market_unavailable'}


def test_paper_accepts_numeric_string_quotes_safely():
    engine=PaperExecutionEngine()
    result=engine.submit(_intent(), 10.0, {'bid':'0.2272','ask':'0.2274','last':0.2273,'ts':1})
    assert result['accepted'] is True
    assert result['filled'] is True
