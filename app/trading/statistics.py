"""Administrative setup measurements, independent of balances and risk state."""
from math import isfinite
from time import time_ns
from app.trading.metrics import position_net_pnl


def _timestamp(value):
    try:
        stamp = float(value)
        return stamp if isfinite(stamp) else 0
    except (TypeError, ValueError):
        return 0


class TradingStatistics:
    def __init__(self, db):
        self.db = db

    def current_period(self, mode):
        rows = self.db.find_many('statistics_periods', {'mode': mode}, limit=1,
                                 sort_field='started_at')
        return rows[0] if rows else None

    def active_positions(self, mode, rows):
        period = self.current_period(mode)
        since = period['started_at'] if period else None
        return [p for p in rows if since is None or _timestamp(p.get('opened_at')) >= since]

    def report(self, mode):
        periods = self.db.find_many('statistics_periods', {'mode': mode}, limit=10,
                                    sort_field='started_at')
        period = periods[0] if periods else None
        since = period['started_at'] if period else None
        rows = self.db.find_many('positions', {'mode': mode}, limit=0)
        selected = self.active_positions(mode, rows)
        closed = [p for p in selected if p.get('status') == 'CLOSED' and not p.get('settlement_pending')]
        pnls = [position_net_pnl(p) for p in closed]
        profit = sum(p for p in pnls if p > 0)
        loss = -sum(p for p in pnls if p < 0)
        wins = sum(p > 0 for p in pnls)
        return {
            'mode': mode,
            'period': self._public(period) if period else None,
            'history': [self._public(p) for p in periods],
            'metrics': {
                'trades': len(pnls), 'wins': wins,
                'losses': sum(p < 0 for p in pnls), 'breakeven': sum(p == 0 for p in pnls),
                'pnl': sum(pnls), 'win_rate': wins / len(pnls) * 100 if pnls else 0,
                'profit_factor': profit / loss if loss else None,
                'open_positions': sum(p.get('status') == 'OPEN' for p in selected),
                'excluded_positions': len(rows) - len(selected),
            },
        }

    def reset(self, mode, label, actor, request_id):
        key = {'reset_id': request_id}
        existing = self.db.find_one('statistics_periods', key)
        if existing:
            if existing['mode'] != mode or existing['actor'] != actor or existing['label'] != label:
                raise ValueError('reset_request_conflict')
            return self._public(existing)
        # A position opened before the cutover may settle afterwards. Refuse a
        # cutover until all fills and settlements in this mode have completed.
        if self.db.count('positions', {'mode': mode, 'status': 'OPEN'}) or self.db.count(
                'positions', {'mode': mode, 'settlement_pending': True}):
            raise ValueError('reset_requires_no_open_positions')
        pending = self.db.find_many('execution_pending', {'active': True}, limit=0)
        for row in pending:
            profile = self.db.find_one('user_trading_profiles', {'user_id': row.get('user_id')}) or {}
            if profile.get('execution_mode', 'demo') == mode:
                raise ValueError('reset_requires_no_pending_orders')
        previous = self.report(mode)
        # Immutable record doubles as durable audit and idempotency receipt.
        record = self.db.set_once('statistics_periods', key, {
            'mode': mode, 'label': label, 'actor': actor,
            'started_at': time_ns() / 1_000_000,
            'previous_metrics': previous['metrics'],
            'previous_reset_id': (previous['period'] or {}).get('reset_id'),
            'global_reset': True,
        })
        if record['mode'] != mode or record['actor'] != actor or record['label'] != label:
            raise ValueError('reset_request_conflict')
        return self._public(record)

    @staticmethod
    def _public(record):
        return {k: v for k, v in record.items() if k != '_id'}
