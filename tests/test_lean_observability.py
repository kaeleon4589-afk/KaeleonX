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


def test_railway_export_safe_line_is_downloadable_text(capfd):
    from app.logging.logger import get_logger

    audit = AuditLogger(None)
    audit.logger = get_logger('kaeleon.audit.test.export_safe')
    audit.event(
        'SIGNAL_REJECTED', 'd-export', user_id='u', mode='demo',
        symbol='BTCUSDT', reason='no_fresh_breakout_retest',
    )
    stdout, stderr = capfd.readouterr()
    assert stderr == ''
    assert 'KAELEON event=SIGNAL_REJECTED payload={' in stdout
    assert '"reason":"no_fresh_breakout_retest"' in stdout


def test_audit_logger_has_separate_stdout_and_stderr_handlers():
    import sys
    from app.logging.logger import get_logger

    logger = get_logger('kaeleon.audit.test.streams')
    stdout_handlers = [h for h in logger.handlers if getattr(h, 'stream', None) is sys.stdout]
    stderr_handlers = [h for h in logger.handlers if getattr(h, 'stream', None) is sys.stderr]
    assert len(stdout_handlers) == 1
    assert len(stderr_handlers) == 1
    assert stderr_handlers[0].level == logging.WARNING
