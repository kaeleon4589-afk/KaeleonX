from __future__ import annotations
import math
from app.models.market import Candle


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def ema(values: list[float], period: int) -> list[float]:
    if not values: return []
    a = 2.0 / (period + 1.0); out=[float(values[0])]
    for v in values[1:]: out.append(a*float(v)+(1-a)*out[-1])
    return out


def true_ranges(candles: list[Candle]) -> list[float]:
    if not candles: return []
    out=[max(candles[0].high-candles[0].low,0.0)]
    for i in range(1,len(candles)):
        x,p=candles[i],candles[i-1]
        out.append(max(x.high-x.low,abs(x.high-p.close),abs(x.low-p.close)))
    return out


def atr(candles: list[Candle], period: int=14) -> float:
    tr=true_ranges(candles)
    if not tr: return 0.0
    w=tr[-period:] if len(tr)>=period else tr
    return sum(w)/len(w)


def adx(candles: list[Candle], period: int=14) -> float:
    if len(candles)<period+2: return 0.0
    trs=[]; plus=[]; minus=[]
    for i in range(1,len(candles)):
        x,p=candles[i],candles[i-1]
        trs.append(max(x.high-x.low,abs(x.high-p.close),abs(x.low-p.close)))
        up=x.high-p.high; dn=p.low-x.low
        plus.append(up if up>dn and up>0 else 0.0); minus.append(dn if dn>up and dn>0 else 0.0)
    def smooth(vals):
        out=[]
        for i in range(len(vals)):
            w=vals[max(0,i-period+1):i+1]; out.append(sum(w)/max(len(w),1))
        return out
    tr_s=smooth(trs); p_s=smooth(plus); m_s=smooth(minus); dx=[]
    for t,p,m in zip(tr_s,p_s,m_s):
        if t<=0: dx.append(0.0); continue
        pi=100*p/t; mi=100*m/t; den=pi+mi
        dx.append(100*abs(pi-mi)/den if den else 0.0)
    w=dx[-period:] if len(dx)>=period else dx
    return sum(w)/max(len(w),1)


def choppiness(candles: list[Candle], period: int=14) -> float:
    if len(candles)<period+1: return 50.0
    w=candles[-period:]; rng=max(x.high for x in w)-min(x.low for x in w)
    if rng<=0: return 50.0
    tr=sum(true_ranges(w))
    return clamp(100*math.log10(max(tr/rng,1e-12))/math.log10(period),0,100)


def efficiency(candles: list[Candle], lookback: int=20) -> float:
    if len(candles)<lookback+1: return 0.0
    c=[x.close for x in candles[-(lookback+1):]]; net=abs(c[-1]-c[0]); path=sum(abs(c[i]-c[i-1]) for i in range(1,len(c)))
    return clamp(net/path if path else 0.0,0,1)


def wick_instability(candles: list[Candle], lookback: int=8) -> float:
    w=candles[-lookback:]; vals=[]
    for x in w:
        r=max(x.high-x.low,1e-12); vals.append(clamp((r-abs(x.close-x.open))/r,0,1))
    return sum(vals)/len(vals) if vals else 0.0


def body_quality(candles: list[Candle], lookback: int=8) -> float:
    w=candles[-lookback:]; vals=[]
    for x in w:
        r=max(x.high-x.low,1e-12); vals.append(clamp(abs(x.close-x.open)/r,0,1))
    return sum(vals)/len(vals) if vals else 0.0


def breakout_failure_ratio(candles: list[Candle], lookback: int=20, sample: int=12) -> float:
    if len(candles)<lookback+3: return 0.0
    total=fail=0; start=max(lookback,len(candles)-sample-1)
    for i in range(start,len(candles)-1):
        prior=candles[i-lookback:i]; hi=max(x.high for x in prior); lo=min(x.low for x in prior); now=candles[i].close; nxt=candles[i+1].close
        if now>hi: total+=1; fail += int(nxt<hi)
        elif now<lo: total+=1; fail += int(nxt>lo)
    return fail/total if total else 0.0


def relative_volume(candles: list[Candle], idx: int=-1, period: int=20) -> float:
    if not candles: return 0.0
    i=idx if idx>=0 else len(candles)+idx
    if i<1 or i>=len(candles): return 0.0
    start=max(0,i-period); base=[max(candles[j].volume,0.0) for j in range(start,i)]
    avg=sum(base)/len(base) if base else 0.0
    return max(candles[i].volume,0.0)/avg if avg>0 else 0.0


def ema_metrics(candles: list[Candle]) -> dict:
    closes=[x.close for x in candles]; e20=ema(closes,20); e50=ema(closes,50); e200=ema(closes,200)
    if not closes: return {}
    p=closes[-1]; a20=e20[-1]; a50=e50[-1]; a200=e200[-1]
    s20=a20-(e20[-6] if len(e20)>=6 else a20); s50=a50-(e50[-6] if len(e50)>=6 else a50)
    bull=p>a20>a50>a200 and s20>0 and s50>=0; bear=p<a20<a50<a200 and s20<0 and s50<=0
    align=1.0 if bull or bear else clamp((.25*(p>a20)) + (.25*(a20>a50)) + (.25*(a50>a200)) + (.125*(s20>0)) + (.125*(s50>0)),0,1)
    return {'ema20':a20,'ema50':a50,'ema200':a200,'ema20_series':e20,'ema50_series':e50,'ema200_series':e200,'slope20':s20,'slope50':s50,'alignment':align,'bullish':bull,'bearish':bear}
