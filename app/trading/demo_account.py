from __future__ import annotations

from datetime import datetime, timezone
import math


class DemoAccount:
    """Balance = immutable opening balance + net realized PnL - trading fees.

    Positions are the idempotent journal: saving the same position twice does
    not book a second gain/loss. Unrealized PnL is shown separately.
    """
    def __init__(self, db, initial_equity=100.0):
        self.db = db
        self.initial_equity = float(initial_equity)

    def opening_balance(self, user_id):
        row = self.db.set_once('demo_accounts', {'user_id': user_id}, {
            'initial_balance': self.initial_equity,
            'created_at': datetime.now(timezone.utc),
        })
        return float(row['initial_balance'])

    def balance(self, user_id):
        value = self.opening_balance(user_id) + self.db.demo_net_pnl(user_id)
        if not math.isfinite(value):
            raise ValueError('invalid_demo_account_balance')
        return max(0.0, value)
