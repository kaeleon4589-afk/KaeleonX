import json
import logging
import os
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
    'WORKER_STARTED',
    'WORKER_STOPPED',
    'ENGINE_HEARTBEAT',
}
_STATE_INFO = {
    'REGIME_EVALUATED',
    'STRATEGY_EVALUATED',
    'SIGNAL_REJECTED',
    'RISK_REJECTED',
    'EXECUTION_REJECTED',
}
_ALWAYS_ERROR = {
    'MARKET_SCAN_ERROR',
    'MARKET_LOOP_ERROR',
    'WORKER_CRASHED',
    'BTC_CONTEXT_ERROR',
    'POSITION_SYNC_ERROR',
    'USER_RUNTIME_ERROR',
    'USER_RUNTIME_CONFIG_ERROR',
    'POSITION_RESTORE_ERROR',
    'TELEGRAM_NOTIFY_FAILED',
}
_THROTTLED_WARNING = {
    'MARKET_SNAPSHOT_ERROR',
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
        # Never dump long indicator/candle/EMA series into Railway logs.  They
        # can contain hundreds of values and add cost without operational value.
        if len(value) > 12:
            return {"_sequence_omitted": True, "count": len(value)}
        return [_redact(v) for v in value]
    if isinstance(value, str) and len(value) > 1200:
        return value[:1200] + "…<truncated>"
    return value


def get_logger(name):
    logger = logging.getLogger(name)
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(h)
        logger.propagate = False
    logger.setLevel(_level())
    return logger


class AuditLogger:
    """Lean structured logs for Railway.

    Operational logs are written to stdout only. They are deliberately *not*
    persisted to MongoDB: business data already lives in positions/orders and
    the latest engine state is an upsert, so duplicating logs in Mongo would add
    cost without improving recovery.
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
        if event in {'SIGNAL_REJECTED', 'RISK_REJECTED', 'EXECUTION_REJECTED'}:
            return str(data.get('reason') or 'unknown')
        if event == 'MARKET_SNAPSHOT_ERROR':
            return str(data.get('error') or 'market_snapshot_error')
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
        repeat = self.reject_repeat_seconds if event in {'SIGNAL_REJECTED', 'RISK_REJECTED', 'EXECUTION_REJECTED', 'MARKET_SNAPSHOT_ERROR'} else self.state_repeat_seconds
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

        record = {
            'ts': datetime.now(timezone.utc).isoformat(),
            'event': event,
            'decision_id': decision_id,
            **_redact(data),
        }
        numeric = getattr(logging, effective, logging.INFO)
        self.logger.log(numeric, json.dumps(record, default=str, separators=(',', ':')))
        return record
