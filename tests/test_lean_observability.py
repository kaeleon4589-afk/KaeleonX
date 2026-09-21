import logging

from app.logging.logger import AuditLogger


class FakeDB:
    def __init__(self): self.writes=[]
    def write(self,*args,**kwargs): self.writes.append((args,kwargs))


def test_audit_logger_never_persists_operational_logs():
    db=FakeDB(); audit=AuditLogger(db)
    audit.logger.setLevel(logging.CRITICAL)
    audit.event('POSITION_OPENED','d1',persist=True,user_id='u',mode='demo',symbol='BTC')
    assert db.writes == []


def test_repeated_regime_is_throttled():
    audit=AuditLogger(None)
    audit.logger.setLevel(logging.CRITICAL)
    first=audit.event('REGIME_EVALUATED','d1',user_id='u',mode='demo',symbol='BTC',state='TREND',candidate='TREND',active='TREND')
    second=audit.event('REGIME_EVALUATED','d2',user_id='u',mode='demo',symbol='BTC',state='TREND',candidate='TREND',active='TREND')
    changed=audit.event('REGIME_EVALUATED','d3',user_id='u',mode='demo',symbol='BTC',state='RANGE',candidate='RANGE',active='RANGE')
    assert first is not None
    assert second is None
    assert changed is not None
