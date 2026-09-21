from __future__ import annotations
from app.models.trading import TradeIntent
from app.models.enums import Direction,Strategy
from app.market.indicators import atr,adx,ema_metrics,relative_volume,clamp

H1_ADX_MIN=12.0; M15_ADX_MIN=11.0; M5_ADX_MIN=9.5; ATR_PCT_MIN=.00075; ATR_PCT_MAX=.0180; RESET_LOOKBACK_BARS=5; RESET_TOUCH_TOL_ATR=.38; RESET_BREAK_TOL_ATR=.48; TRIGGER_MAX_EMA20_EXTENSION_ATR=.95; SL_MIN=.0045; SL_MAX=.0068; SL_ATR_MULT=.88; SL_BUFFER_ATR=.10; TP_MIN=.0060; TP_MAX=.0085; MIN_SCORE=69.0

def _bias(cs,adx_min):
    if len(cs)<60:return 'none',{}
    e=ema_metrics(cs); a=adx(cs); p=cs[-1].close; spread=abs(e.get('ema20',p)-e.get('ema200',p))/max(p,1e-12)
    if a<adx_min:return 'none',{'adx':a,'stack_spread':spread}
    if e.get('bullish') or (p>e.get('ema20',p)>e.get('ema50',p) and e.get('slope20',0)>0): return 'long',{'adx':a,'stack_spread':spread}
    if e.get('bearish') or (p<e.get('ema20',p)<e.get('ema50',p) and e.get('slope20',0)<0): return 'short',{'adx':a,'stack_spread':spread}
    return 'none',{'adx':a,'stack_spread':spread}

class BreakoutRetestStrategy:
    def evaluate(self,regime,candles,decision_id,symbol,timeframe,current_price=None,snapshot=None):
        if regime.hard_block or not regime.breakout_allowed:return None
        tfs=getattr(snapshot,'timeframes',{}) if snapshot is not None else {}; c5=tfs.get('5m',candles); c15=tfs.get('15m',[]); c1=tfs.get('1h',[])
        if len(c5)<60 or len(c15)<60 or len(c1)<60:return None
        p=float(current_price or c5[-1].close); a=atr(c5); ap=a/max(p,1e-12); a5=adx(c5)
        if not (ATR_PCT_MIN<=ap<=ATR_PCT_MAX) or a5<M5_ADX_MIN:return None
        b1,d1=_bias(c1,H1_ADX_MIN); b15,d15=_bias(c15,M15_ADX_MIN)
        if b1=='none' or b1!=b15:return None
        e=ema_metrics(c5); e20=e.get('ema20',p); e50=e.get('ema50',p); e200=e.get('ema200',p); last=c5[-1]; prev=c5[-2]
        recent=c5[-(RESET_LOOKBACK_BARS+2):-1]
        if b1=='long':
            touched=any(x.low<=e20+a*RESET_TOUCH_TOL_ATR and x.low>=e50-a*RESET_BREAK_TOL_ATR for x in recent); continuation=last.close>prev.high-a*.10 and last.close>e20; extension=(last.close-e20)/max(a,1e-12)
            if not touched or not continuation or extension>TRIGGER_MAX_EMA20_EXTENSION_ATR or not (last.close>e20>e50>e200):return None
            extreme=min(x.low for x in recent); structural=(p-extreme)/max(p,1e-12); direction=Direction.LONG
        else:
            touched=any(x.high>=e20-a*RESET_TOUCH_TOL_ATR and x.high<=e50+a*RESET_BREAK_TOL_ATR for x in recent); continuation=last.close<prev.low+a*.10 and last.close<e20; extension=(e20-last.close)/max(a,1e-12)
            if not touched or not continuation or extension>TRIGGER_MAX_EMA20_EXTENSION_ATR or not (last.close<e20<e50<e200):return None
            extreme=max(x.high for x in recent); structural=(extreme-p)/max(p,1e-12); direction=Direction.SHORT
        h1q=clamp((d1['adx']-H1_ADX_MIN)/15,0,1); m15q=clamp((d15['adx']-M15_ADX_MIN)/14,0,1); m5q=clamp((a5-M5_ADX_MIN)/13,0,1); align=clamp((d1['stack_spread']+d15['stack_spread'])/(.00030*6),0,1); resetq=clamp(1-extension/TRIGGER_MAX_EMA20_EXTENSION_ATR,0,1); rv=relative_volume(c5)
        quality=clamp(.30*h1q+.25*m15q+.20*m5q+.15*align+.10*resetq,0,1); score=min(100,69+28*quality+min(max(rv-1,0)*2,3))
        if score<MIN_SCORE:return None
        sl_pct=clamp(max(ap*SL_ATR_MULT,structural+ap*SL_BUFFER_ATR),SL_MIN,SL_MAX); tp_pct=clamp(.0060+(score-69)*.00007,TP_MIN,TP_MAX)
        stop=p*(1-sl_pct) if direction==Direction.LONG else p*(1+sl_pct); target=p*(1+tp_pct) if direction==Direction.LONG else p*(1-tp_pct)
        return TradeIntent(decision_id,symbol,Strategy.BREAKOUT_RETEST,direction,p,stop,target,score,regime.risk_multiplier,timeframe,('mtf_1h_15m_alignment','5m_reset_retest','continuation_confirmed'),{'strategy_model':'mtf_simple_continuation_5m_v3_pure','score':score,'atr_pct':ap,'adx5':a5,'adx15':d15['adx'],'adx1h':d1['adx'],'rvol':rv,'sl_pct':sl_pct,'tp_pct':tp_pct,'partial_tp_enabled':False,'tp2_price':target,'break_even_activation_pct':clamp(max(sl_pct*.45,tp_pct*.38),.0028,.0042)})
