from types import SimpleNamespace

from app.logging.logger import AuditLogger
from app.strategy.breakout_retest import BreakoutRetestStrategy
from app.strategy.liquidity_sweep import LiquiditySweepStrategy


class FakeDB:
    def __init__(self): self.rows=[]
    def write(self, collection, document): self.rows.append((collection, dict(document)))


def test_audit_redacts_secrets_without_persisting_operational_logs():
    db = FakeDB()
    audit = AuditLogger(db)
    row = audit.event('TEST_EVENT', 'd1', user_id='u1', mode='demo', api_secret='very-secret-value', nested={'access_token':'abcdefghi'})
    assert row['api_secret'] != 'very-secret-value'
    assert row['nested']['access_token'] != 'abcdefghi'
    assert db.rows == []


def test_breakout_trace_explains_rejection():
    strategy = BreakoutRetestStrategy()
    regime = SimpleNamespace(hard_block=True, breakout_allowed=False)
    assert strategy.evaluate(regime, [], 'd', 'BTC', '5m') is None
    assert strategy.last_trace['reason'] == 'regime_hard_block'


def test_sweep_trace_explains_rejection():
    strategy = LiquiditySweepStrategy()
    regime = SimpleNamespace(hard_block=False, sweep_allowed=True)
    assert strategy.evaluate(regime, [], 'd', 'BTC', '5m') is None
    assert strategy.last_trace['reason'] == 'insufficient_bars'
