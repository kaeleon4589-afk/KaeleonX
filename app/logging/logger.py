import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

_SENSITIVE_KEYS = {
    'api_key', 'api_secret', 'authorization', 'credential_encryption_key',
    'telegram_bot_token', 'telegram_webhook_secret', 'private_key', 'secret',
    'password', 'token', 'access_token', 'refresh_token',
}

# Production logging is intentionally lean. These events are useful enough to
# keep visible at INFO; everything else is demoted to DEBUG unless it is an error.
_ALWAYS_INFO = {
    'SIGNAL_ACCEPTED',
    'POSITION_OPENED',
    'POSITION_CLOSED',
    'RISK_REJECTED',
    'EXECUTION_REJECTED',
    'EXECUTION_PENDING',
    'EXECUTION_PENDING_CLEARED',
    'PIPELINE_ERROR',
    'WORKER_STARTED',
    'WORKER_STOPPED',
    'ENGINE_HEARTBEAT',
}
_STATE_INFO = {
    'REGIME_EVALUATED',
    'STRATEGY_EVALUATED',
    'SIGNAL_REJECTED',
    'MARKET_DATA_SKIPPED',
}
_ALWAYS_ERROR = {
    'MARKET_LOOP_ERROR',
    'WORKER_CRASHED',
    'POSITION_SYNC_ERROR',
    'USER_RUNTIME_ERROR',
    'USER_RUNTIME_CONFIG_ERROR',
    'POSITION_RESTORE_ERROR',
    'TELEGRAM_NOTIFY_FAILED',
    'POSITION_PERSIST_ERROR',
    'ORDER_PERSIST_ERROR',
    'DECISION_PERSIST_ERROR',
    'POSITION_OPEN_CALLBACK_ERROR',
    'POSITION_CLOSE_CALLBACK_ERROR',
}
_THROTTLED_WARNING = {
    'MARKET_SNAPSHOT_ERROR',
    'MARKET_SCAN_FAILSAFE_CACHE',
    'MARKET_SCAN_ERROR',
    'BTC_CONTEXT_ERROR',
    'MARKET_DEPTH_ERROR',
    'MARKET_INSTRUMENTS_ERROR',
}
_DEBUG_ONLY = {
    'DECISION_START',
    'RISK_EVALUATED',
    'EXECUTION_RESULT',
    'MARKET_SCAN_DONE',
    'MARKET_SCAN_CACHE_HIT',
    'ENTRY_SKIPPED',
    'ENTRY_BLOCKED',
    'USER_MARKET_ANALYSIS_START',
    'TELEGRAM_NOTIFY_QUEUED',
    'TELEGRAM_NOTIFY_SENT',
    'TELEGRAM_NOTIFY_SKIPPED',
}


def _level() -> int:
    raw = os.getenv('LOG_LEVEL', 'INFO').upper().strip()
    return getattr(logging, raw, logging.INFO)


def _redact(value, key: str | None = None):
    if key and any(part in key.lower() for part in _SENSITIVE_KEYS):
        if value in (None, ''):
            return value
        text = str(value)
        if len(text) <= 6:
            return '***'
        return f'{text[:2]}***{text[-2:]}'
    if isinstance(value, dict):
        return {k: _redact(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        # Never dump long indicator/candle/EMA series into Railway logs. They
        # can contain hundreds of values and add cost without operational value.
        if len(value) > 12:
            return {"_sequence_omitted": True, "count": len(value)}
        return [_redact(v) for v in value]
    if isinstance(value, str) and len(value) > 1200:
        return value[:1200] + "…<truncated>"
    return value


def _railway_level(effective: str) -> str:
    """Return one of Railway's canonical structured-log severities."""
    level = str(effective or 'INFO').upper()
    if level == 'DEBUG':
        return 'debug'
    if level in {'WARNING', 'WARN'}:
        return 'warn'
    if level in {'ERROR', 'CRITICAL'}:
        return 'error'
    return 'info'


def _message_value(value, *, limit: int = 260) -> str:
    if isinstance(value, (list, tuple)):
        text = ','.join(str(v) for v in value[:8])
        if len(value) > 8:
            text += f',…(+{len(value) - 8})'
    elif isinstance(value, dict):
        text = json.dumps(value, default=str, separators=(',', ':'))
    else:
        text = str(value)
    if len(text) > limit:
        return text[:limit] + '…'
    return text


def _build_message(event: str, data: dict) -> str:
    """Build a compact human-readable message for Railway exports.

    Railway displays the ``message`` field as the visible log text. Keep it
    concise while retaining the fields needed to diagnose the trading pipeline.
    All values passed here have already gone through secret redaction.
    """
    preferred = (
        'symbol', 'mode', 'strategy', 'direction', 'state', 'candidate',
        'reason', 'stage', 'api_code', 'endpoint', 'markets_scanned', 'market',
        'quality', 'execution_rr', 'orderbook_valid', 'error',
    )
    parts = [str(event)]
    for key in preferred:
        value = data.get(key)
        if value is None or value == '':
            continue
        parts.append(f'{key}={_message_value(value)}')

    top_symbols = data.get('top_symbols')
    if top_symbols:
        parts.append(f'top_symbols={_message_value(top_symbols)}')

    return ' | '.join(parts)


def get_logger(name):
    """Return a lean logger that writes application output to stdout.

    Python's StreamHandler defaults to stderr, which Railway classifies as an
    error stream. KAELEON operational logs are normal application output, so
    they must go to stdout; severity is carried explicitly by structured audit
    records.
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        h = logging.StreamHandler(stream=sys.stdout)
        h.setFormatter(logging.Formatter('%(message)s'))
        h._kaeleon_stdout_handler = True  # type: ignore[attr-defined]
        logger.addHandler(h)
        logger.propagate = False
    logger.setLevel(_level())
    return logger


class AuditLogger:
    """Lean Railway-native structured logs.

    Every audit record is emitted as a single JSON line with Railway's expected
    ``message`` and ``level`` fields. Remaining fields stay queryable as custom
    attributes (for example ``@decision_id:...`` or ``@symbol:BTC``).

    Operational logs are deliberately *not* persisted to MongoDB: business data
    already lives in positions/orders and the latest engine state is an upsert,
    so duplicating logs in Mongo would add cost without improving recovery.
    """

    def __init__(self, db=None):
        self.db = db  # kept for compatibility; no log persistence is performed
        self.logger = get_logger('kaeleon.audit')
        self._last_state: dict[tuple, tuple[str, float]] = {}
        self.state_repeat_seconds = int(os.getenv('LOG_STATE_REPEAT_SECONDS', '300'))
        self.reject_repeat_seconds = int(os.getenv('LOG_REJECT_REPEAT_SECONDS', '300'))

    @staticmethod
    def _state_signature(event: str, data: dict) -> str:
        if event == 'REGIME_EVALUATED':
            return '|'.join(str(data.get(k) or '') for k in ('state', 'candidate', 'active'))
        if event == 'STRATEGY_EVALUATED':
            trace = data.get('trace') or {}
            selected = data.get('strategy') or (trace.get('selected') if isinstance(trace, dict) else None)
            reason = trace.get('reason') if isinstance(trace, dict) else None
            return f'{selected or "NONE"}|{reason or ""}'
        if event in {'SIGNAL_REJECTED', 'MARKET_DATA_SKIPPED'}:
            return str(data.get('reason') or 'unknown')
        if event in {'MARKET_SNAPSHOT_ERROR', 'MARKET_DEPTH_ERROR',
                     'MARKET_INSTRUMENTS_ERROR', 'BTC_CONTEXT_ERROR', 'MARKET_SCAN_ERROR'}:
            return '|'.join(str(data.get(k) or '') for k in ('stage', 'api_code', 'endpoint', 'error'))
        return ''

    def _should_emit_state(self, event: str, data: dict) -> bool:
        key = (
            event,
            str(data.get('user_id') or 'system'),
            str(data.get('mode') or ''),
            str(data.get('symbol') or ''),
        )
        signature = self._state_signature(event, data)
        now = time.monotonic()
        previous = self._last_state.get(key)
        repeat = self.reject_repeat_seconds if event in {'SIGNAL_REJECTED', 'MARKET_DATA_SKIPPED', 'MARKET_SNAPSHOT_ERROR', 'MARKET_DEPTH_ERROR', 'MARKET_INSTRUMENTS_ERROR', 'BTC_CONTEXT_ERROR', 'MARKET_SCAN_ERROR'} else self.state_repeat_seconds
        if previous and previous[0] == signature and now - previous[1] < repeat:
            return False
        self._last_state[key] = (signature, now)
        return True

    def event(self, event, decision_id=None, *, level='INFO', persist=False, **data):
        # persist is intentionally ignored. Keeping the argument avoids touching
        # every caller and makes the production behavior explicit.
        requested = str(level).upper()
        if event in _ALWAYS_ERROR or requested in {'ERROR', 'CRITICAL'}:
            effective = requested if requested in {'ERROR', 'CRITICAL'} else 'ERROR'
        elif event in _THROTTLED_WARNING:
            if not self._should_emit_state(event, data):
                return None
            effective = 'WARNING'
        elif requested == 'DEBUG' or event in _DEBUG_ONLY:
            effective = 'DEBUG'
        elif event in _ALWAYS_INFO:
            effective = 'INFO'
        elif event in _STATE_INFO:
            if not self._should_emit_state(event, data):
                return None
            effective = 'INFO'
        else:
            effective = 'DEBUG'

        safe_data = _redact(data)
        record = {
            **safe_data,
            'ts': datetime.now(timezone.utc).isoformat(),
            'event': event,
            'decision_id': decision_id,
            'level': _railway_level(effective),
            'message': _build_message(event, safe_data),
        }
        numeric = getattr(logging, effective, logging.INFO)
        # One minified JSON object per line is required for Railway structured
        # logging. The handler writes to stdout, so INFO records no longer appear
        # as empty/red stderr entries in exported logs.
        self.logger.log(numeric, json.dumps(record, default=str, separators=(',', ':')))
        return record
