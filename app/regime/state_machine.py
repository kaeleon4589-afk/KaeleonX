from __future__ import annotations
DEFAULT_CONFIRM_BARS=3; DEFAULT_COOLDOWN_BARS=2; DEFAULT_MIN_ACTIVE_BARS=3; UNKNOWN='UNKNOWN'

def advance(candidate, prev=None, confirm_bars=3, cooldown_bars=2, min_active_bars=3):
    p=dict(prev or {}); active=str(p.get('active') or UNKNOWN); pending=str(p.get('pending') or ''); count=int(p.get('pending_count') or 0); bars=int(p.get('bars') or 0); cooldown=max(int(p.get('cooldown') or 0)-1,0); changed=False
    candidate=str(candidate or UNKNOWN)
    if active==UNKNOWN and bars<=0:
        active=candidate if candidate!=UNKNOWN else UNKNOWN; bars=1; pending=''; count=0
    elif candidate==active:
        bars+=1; pending=''; count=0
    else:
        if candidate==pending: count+=1
        else: pending=candidate; count=1
        can=(cooldown<=0 and bars>=min_active_bars) or active==UNKNOWN
        req=1 if active==UNKNOWN and candidate!=UNKNOWN else confirm_bars
        if can and count>=req:
            active=candidate; bars=1; cooldown=cooldown_bars; pending=''; count=0; changed=True
        else: bars+=1
    return {'active':active,'candidate':candidate,'pending':pending,'pending_count':count,'bars':bars,'cooldown':cooldown,'changed':changed}
