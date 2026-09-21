from app.market.indicators import atr,adx,choppiness,efficiency,ema_metrics,relative_volume
from app.models.market import Candle
from app.regime.advanced import features,classify,TREND,VOLATILE,RANGE,UNKNOWN
from app.regime.state_machine import advance
from app.strategy.breakout_retest import BreakoutRetestStrategy
from app.strategy.liquidity_sweep import LiquiditySweepStrategy
from app.market.scanner import CoinWMarketScanner


def trend_candles(n=300, start=100.0, step=.12):
    out=[]
    for i in range(n):
        o=start+i*step; c=o+step*.8; out.append(Candle(i*300000,o,o+.18,o-.06,c,1000+i*2))
    return out

def test_indicators_and_regime_features_are_finite():
    c=trend_candles(); f=features(c,c)
    assert f['context_ok']; assert atr(c)>0; assert adx(c)>=0; assert 0<=choppiness(c)<=100; assert 0<=efficiency(c)<=1
    regime,conf,scores=classify(f); assert regime in {TREND,VOLATILE,RANGE,UNKNOWN}; assert 0<=conf<=1; assert scores

def test_regime_state_requires_confirmation_after_active():
    s=advance(TREND,None); assert s['active']==TREND
    s=advance(VOLATILE,s); assert s['active']==TREND
    s=advance(VOLATILE,s); assert s['active']==TREND
    s=advance(VOLATILE,s); assert s['active']==VOLATILE

def test_scanner_scores_and_blocks_memes():
    class C:
        async def tickers(self):
            return {'data':[{'instrument':'BTCUSDT','last':'60000','volume':'2000000','change24h':'0.03'},{'instrument':'PEPEUSDT','last':'1','volume':'99999999','change24h':'0.2'}]}
    import asyncio
    rows=asyncio.run(CoinWMarketScanner(C(),depth=10,cache_seconds=1).ranked())
    assert rows and rows[0]['symbol']=='BTC'; assert all('PEPE' not in r['symbol'] for r in rows)
