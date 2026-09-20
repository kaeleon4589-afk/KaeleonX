from app.position.tp_sl import TpSlEngine
from app.models.enums import Direction

def test_long_plan():
    p=TpSlEngine().build('LIQUIDITY_SWEEP',Direction.LONG,100,95,2,min_rr2=1.8)
    assert p.stop_price<100<p.tp1_price<p.tp2_price
    assert p.rr1==1.0 and p.rr2>=1.8

def test_short_plan():
    p=TpSlEngine().build('BREAKOUT_RETEST',Direction.SHORT,100,105,2,min_rr2=1.8)
    assert p.tp2_price<p.tp1_price<100<p.stop_price
