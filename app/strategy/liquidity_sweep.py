from __future__ import annotations
from app.models.trading import TradeIntent
from app.models.enums import Direction,Strategy
from app.market.indicators import atr,ema_metrics,relative_volume,clamp
SWEEP_LOOKBACK=34; SWEEP_MAX_AGE=8; MIN_DEPTH=.10; MIN_WICK=.28; MIN_SWEEP_RVOL=.58; MIN_TRIGGER_RVOL=.48; MIN_BODY=.12; EXT_MAX=2.25; SL_MIN=.005; SL_MAX=.007; TP_MIN=.005; TP_MAX=.007; ATR_MIN=.0013; ATR_MAX=.028; MIN_STRUCTURAL_RR=.95

class LiquiditySweepStrategy:
    def __init__(self): self.last_trace={}
    def _reject(self,reason,**metrics): self.last_trace={'accepted':False,'reason':reason,**metrics}; return None
    def evaluate(self,regime,candles,decision_id,symbol,timeframe,current_price=None,snapshot=None):
        self.last_trace={'accepted':False,'reason':'not_evaluated'}
        if regime.hard_block:return self._reject('regime_hard_block')
        if not regime.sweep_allowed:return self._reject('regime_sweep_not_allowed')
        c=getattr(snapshot,'timeframes',{}).get('5m',candles) if snapshot is not None else candles
        if len(c)<SWEEP_LOOKBACK+12:return self._reject('insufficient_bars',bars=len(c),required=SWEEP_LOOKBACK+12)
        p=float(current_price or c[-1].close); a=atr(c); ap=a/max(p,1e-12)
        if not (ATR_MIN<=ap<=ATR_MAX):return self._reject('atr_pct_out_of_range',atr_pct=ap,min=ATR_MIN,max=ATR_MAX)
        e=ema_metrics(c); trigger=c[-1]; best=None; near=[]
        start=max(SWEEP_LOOKBACK,len(c)-1-SWEEP_MAX_AGE)
        for i in range(start,len(c)-1):
            prior=c[i-SWEEP_LOOKBACK:i]; hi=max(x.high for x in prior); lo=min(x.low for x in prior); x=c[i]; age=len(c)-1-i
            rng=max(x.high-x.low,1e-12); rv=relative_volume(c,i)
            if x.low<lo and x.close>=lo-a*.36:
                depth=(lo-x.low)/max(a,1e-12); wick=(min(x.open,x.close)-x.low)/rng; trv=relative_volume(c); tb=abs(trigger.close-trigger.open)/max(trigger.high-trigger.low,1e-12); closepos=(trigger.close-trigger.low)/max(trigger.high-trigger.low,1e-12); ext=abs(trigger.close-e.get('ema20',trigger.close))/max(a,1e-12)
                checks={'depth':depth,'wick':wick,'sweep_rvol':rv,'trigger_rvol':trv,'trigger_body':tb,'close_position':closepos,'extension_atr':ext,'age':age,'side':'long'}; near.append(checks)
                valid=depth>=MIN_DEPTH and wick>=MIN_WICK and rv>=MIN_SWEEP_RVOL and trv>=MIN_TRIGGER_RVOL and tb>=MIN_BODY and closepos>=.46 and ext<=EXT_MAX and trigger.close>lo
                if valid:
                    rr=max((max(z.high for z in c[max(0,i-48):i])-p)/max(p-(x.low-a*.18),1e-12),0); 
                    if rr < MIN_STRUCTURAL_RR:
                        continue
                    score=self._score(depth,wick,rv,trv,tb,closepos,ext,rr,age); cand=('long',score,x.low-a*.18,lo,depth,wick,rv,trv,rr,age)
                    if best is None or score>best[1]:best=cand
            if x.high>hi and x.close<=hi+a*.36:
                depth=(x.high-hi)/max(a,1e-12); wick=(x.high-max(x.open,x.close))/rng; trv=relative_volume(c); tb=abs(trigger.close-trigger.open)/max(trigger.high-trigger.low,1e-12); closepos=(trigger.high-trigger.close)/max(trigger.high-trigger.low,1e-12); ext=abs(trigger.close-e.get('ema20',trigger.close))/max(a,1e-12)
                checks={'depth':depth,'wick':wick,'sweep_rvol':rv,'trigger_rvol':trv,'trigger_body':tb,'close_position':closepos,'extension_atr':ext,'age':age,'side':'short'}; near.append(checks)
                valid=depth>=MIN_DEPTH and wick>=MIN_WICK and rv>=MIN_SWEEP_RVOL and trv>=MIN_TRIGGER_RVOL and tb>=MIN_BODY and closepos>=.46 and ext<=EXT_MAX and trigger.close<hi
                if valid:
                    rr=max((p-min(z.low for z in c[max(0,i-48):i]))/max((x.high+a*.18)-p,1e-12),0); 
                    if rr < MIN_STRUCTURAL_RR:
                        continue
                    score=self._score(depth,wick,rv,trv,tb,closepos,ext,rr,age); cand=('short',score,x.high+a*.18,hi,depth,wick,rv,trv,rr,age)
                    if best is None or score>best[1]:best=cand
        if not best:
            details=max(near,key=lambda z:z.get('depth',0)+z.get('wick',0)) if near else {}
            failed=[]
            if details:
                if details['depth']<MIN_DEPTH:failed.append('depth')
                if details['wick']<MIN_WICK:failed.append('wick')
                if details['sweep_rvol']<MIN_SWEEP_RVOL:failed.append('sweep_rvol')
                if details['trigger_rvol']<MIN_TRIGGER_RVOL:failed.append('trigger_rvol')
                if details['trigger_body']<MIN_BODY:failed.append('trigger_body')
                if details['close_position']<.46:failed.append('close_position')
                if details['extension_atr']>EXT_MAX:failed.append('extension')
            return self._reject('no_valid_sweep',failed_checks=failed,nearest=details)
        if best[1]<74:return self._reject('score_too_low',score=best[1],min=74)
        side,score,struct_stop,level,depth,wick,srv,trv,rr,age=best; direction=Direction.LONG if side=='long' else Direction.SHORT
        structural=abs(p-struct_stop)/max(p,1e-12); sl_pct=clamp(structural,SL_MIN,SL_MAX); tp_pct=clamp(.0059+clamp((score-84)*.00005,-.00022,.00022)+clamp((min(rr,2)-1)*.00025,-.0001,.00018)-clamp((age-1)*.0001,0,.00035),TP_MIN,TP_MAX)
        stop=p*(1-sl_pct) if direction==Direction.LONG else p*(1+sl_pct); target=p*(1+tp_pct) if direction==Direction.LONG else p*(1-tp_pct)
        risk_abs=abs(p-stop); reward_abs=abs(target-p); execution_rr=reward_abs/max(risk_abs,1e-12)
        if not (execution_rr > 0):
            return self._reject('invalid_execution_rr',execution_rr=execution_rr)
        self.last_trace={'accepted':True,'reason':'setup_valid','score':score,'side':side,'sweep_level':level,'depth_atr':depth,'wick_ratio':wick,'sweep_rvol':srv,'trigger_rvol':trv,'structural_rr':rr,'execution_rr':execution_rr,'age':age,'sl_pct':sl_pct,'tp_pct':tp_pct,'entry':p,'stop':stop,'target':target}
        return TradeIntent(decision_id,symbol,Strategy.LIQUIDITY_SWEEP,direction,p,stop,target,score,regime.risk_multiplier,timeframe,('liquidity_swept','level_reclaimed','trigger_confirmed'),{'strategy_model':'liquidity_sweep_reversal_5m_v1','score':score,'sweep_level':level,'sweep_depth_atr':depth,'sweep_wick_ratio':wick,'sweep_rvol':srv,'trigger_rvol':trv,'bars_since_sweep':age,'structural_rr_estimate':rr,'rr_estimate':execution_rr,'execution_rr':execution_rr,'sl_pct':sl_pct,'tp_pct':tp_pct,'partial_tp_enabled':False,'tp2_price':target,'break_even_activation_pct':clamp(max(sl_pct*.46,tp_pct*.40),.0028,.0039)})
    def _score(self,depth,wick,srv,trv,body,closepos,ext,rr,age):
        q=clamp(.19*clamp((depth-MIN_DEPTH)/.70,0,1)+.16*clamp((wick-MIN_WICK)/.42,0,1)+.12*clamp((srv-.85)/.90,0,1)+.13*clamp((trv-.80)/.85,0,1)+.14*clamp((body-MIN_BODY)/.45,0,1)+.10*clamp((closepos-.50)/.40,0,1)+.18*clamp((rr-.95)/1.4,0,1)-.08*clamp(ext/EXT_MAX,0,1)-.06*clamp((age-1)/max(SWEEP_MAX_AGE-1,1),0,1),0,1)
        return min(100,66+34*q)
