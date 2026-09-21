from __future__ import annotations
from app.market.indicators import atr,adx,choppiness,efficiency,wick_instability,body_quality,breakout_failure_ratio,ema_metrics,clamp

TREND='TREND_CONTINUATION'; VOLATILE='VOLATILE_SWEEP'; RANGE='RANGE'; UNKNOWN='UNKNOWN'

def features(candles, btc_candles=None):
    if not candles: return {'context_ok':False}
    p=candles[-1].close; a=atr(candles); ep=ema_metrics(candles); atr_pct=a/max(p,1e-12)
    def move(cs,bars=3):
        if not cs or len(cs)<bars+1:return 0.0
        return (cs[-1].close-cs[-bars-1].close)/max(cs[-bars-1].close,1e-12)
    btc=btc_candles or candles; ba=atr(btc); bp=btc[-1].close if btc else 0; b_atr_pct=ba/max(bp,1e-12) if bp else 0; bm=abs(move(btc,3))
    return {'context_ok':len(candles)>=30,'adx':adx(candles),'choppiness':choppiness(candles),'efficiency_ratio':efficiency(candles),'wick_instability':wick_instability(candles),'body_quality':body_quality(candles),'breakout_failure_ratio':breakout_failure_ratio(candles),'atr':a,'atr_pct':atr_pct,'ema_stack_alignment':ep.get('alignment',0),'trend_bias':'long' if ep.get('bullish') else ('short' if ep.get('bearish') else 'neutral'),'recent_move_3':abs(move(candles,3)),'btc_recent_move_3':bm,'btc_shock_ratio':bm/max(b_atr_pct,1e-12) if b_atr_pct else 0.0,'ema':ep}

def classify(f):
    if not f.get('context_ok'): return UNKNOWN,0.0,{}
    ad=float(f['adx']); chop=float(f['choppiness']); eff=float(f['efficiency_ratio']); wick=float(f['wick_instability']); fail=float(f['breakout_failure_ratio']); ap=float(f['atr_pct']); shock=float(f['btc_shock_ratio']); align=float(f['ema_stack_alignment']); body=float(f['body_quality'])
    trend=sum([ad>=15,chop<=57,eff>=.23,align>=.54,fail<=.26,body>=.35]); vol=sum([shock>=1.20,wick>=.48,fail>=.18,ap>=.006,body<=.45 and eff<=.48,f['recent_move_3']>=max(ap*.55,.0035) or f['btc_recent_move_3']>=.0035]); rng=sum([ad<=22,chop>=51,eff<=.38,align<=.68,fail>=.07 or wick>=.42])
    scores={TREND:int(trend),VOLATILE:int(vol),RANGE:int(rng)}; best=max(scores,key=scores.get); ordered=sorted(scores.values(),reverse=True); margin=ordered[0]-(ordered[1] if len(ordered)>1 else 0)
    if scores[best]<3: return UNKNOWN,.20,scores
    conf=min(.99,.40+.07*scores[best]+.03*max(0,margin)); return best,conf,scores
