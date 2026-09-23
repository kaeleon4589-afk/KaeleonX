import io
import json
import logging

from app.logging.logger import AuditLogger, _build_message, _railway_level, get_logger


def _capture_audit_line(monkeypatch, event='ENGINE_HEARTBEAT', **data):
    monkeypatch.setenv('LOG_LEVEL', 'INFO')
    logger = logging.getLogger('kaeleon.audit')
    old_handlers = list(logger.handlers)
    old_level = logger.level
    old_propagate = logger.propagate
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter('%(message)s'))
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        AuditLogger().event(event, 'decision-123', **data)
        return stream.getvalue().strip()
    finally:
        logger.handlers = old_handlers
        logger.level = old_level
        logger.propagate = old_propagate


def test_railway_structured_log_has_message_and_level(monkeypatch):
    line = _capture_audit_line(
        monkeypatch,
        markets_scanned=12,
        top_symbols=['BTC', 'ETH', 'SOL'],
    )
    row = json.loads(line)
    assert row['event'] == 'ENGINE_HEARTBEAT'
    assert row['decision_id'] == 'decision-123'
    assert row['level'] == 'info'
    assert row['message'].startswith('ENGINE_HEARTBEAT')
    assert 'markets_scanned=12' in row['message']
    assert 'top_symbols=BTC,ETH,SOL' in row['message']


def test_warning_maps_to_railway_warn(monkeypatch):
    line = _capture_audit_line(
        monkeypatch,
        event='MARKET_SNAPSHOT_ERROR',
        symbol='CAP',
        stage='5m',
        error='coinw_public_api_error',
    )
    row = json.loads(line)
    assert row['level'] == 'warn'
    assert 'symbol=CAP' in row['message']
    assert 'stage=5m' in row['message']
    assert 'error=coinw_public_api_error' in row['message']


def test_sensitive_fields_stay_redacted_in_message_and_attributes(monkeypatch):
    line = _capture_audit_line(
        monkeypatch,
        event='PIPELINE_ERROR',
        symbol='BTC',
        error='failed safely',
        api_secret='super-secret-value',
    )
    row = json.loads(line)
    assert row['api_secret'] != 'super-secret-value'
    assert 'super-secret-value' not in line


def test_railway_level_contract():
    assert _railway_level('DEBUG') == 'debug'
    assert _railway_level('INFO') == 'info'
    assert _railway_level('WARNING') == 'warn'
    assert _railway_level('ERROR') == 'error'
    assert _railway_level('CRITICAL') == 'error'


def test_build_message_is_compact():
    message = _build_message('POSITION_OPENED', {
        'symbol': 'BTC',
        'mode': 'demo',
        'strategy': 'BREAKOUT_RETEST',
        'direction': 'LONG',
        'quality': 92.5,
        'execution_rr': 1.2,
    })
    assert message == (
        'POSITION_OPENED | symbol=BTC | mode=demo | strategy=BREAKOUT_RETEST | '
        'direction=LONG | quality=92.5 | execution_rr=1.2'
    )


def test_get_logger_uses_stdout(monkeypatch):
    monkeypatch.setenv('LOG_LEVEL', 'INFO')
    name = 'kaeleon.test.stdout.contract'
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger = get_logger(name)
    assert len(logger.handlers) == 1
    assert logger.handlers[0].stream is __import__('sys').stdout
