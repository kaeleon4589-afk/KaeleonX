from app.models.enums import Direction
from app.models.trading import Position


class PositionManager:
    def __init__(self, exit_engine, audit=None, on_realized=None, db=None,
                 evaluate_local_exits=True, owner_user_id=None):
        self.exit_engine = exit_engine
        self.positions = {}
        self.audit = audit
        self.on_realized = on_realized
        self.db = db
        self.evaluate_local_exits = evaluate_local_exits
        self.owner_user_id = owner_user_id

    def add(self, position):
        if position.remaining_quantity is None:
            position.remaining_quantity = position.quantity
        self.positions[position.position_id] = position
        self._persist(position)
        return position

    def restore(self, positions):
        for p in positions:
            self.add(p)

    def mark(self, symbol, price, timestamp):
        for p in list(self.positions.values()):
            if p.symbol != symbol or p.status != 'OPEN':
                continue
            qty = p.remaining_quantity or p.quantity
            p.unrealized_pnl = self._pnl(p, price, qty)
            if self.evaluate_local_exits:
                action = self.exit_engine.evaluate(p, price)
                if action:
                    self.close_or_reduce(p, action, price, timestamp)
                    continue
            self._persist(p)

    def reconcile_exchange(self, exchange_positions, timestamp):
        """Make local OPEN state follow a successful CoinW position snapshot.

        Exchange state is authoritative in live mode. An empty successful response
        closes local positions that no longer exist and imports unknown open positions.
        """
        rows = [x for x in (exchange_positions or [])
                if isinstance(x, dict) and str(x.get('status', '')).lower() == 'open']
        exchange = {}
        for row in rows:
            pid = str(row.get('id') or row.get('openId') or '')
            if pid:
                exchange[pid] = row

        local_open = {
            pid: p for pid, p in self.positions.items()
            if p.status == 'OPEN'
        }

        for pid, p in local_open.items():
            row = exchange.get(pid)
            if row is None:
                p.status = 'CLOSED'
                p.closed_at = timestamp
                p.exit_reason = 'EXCHANGE_CLOSED'
                p.remaining_quantity = 0.0
                p.unrealized_pnl = 0.0
                self._persist(p)
                continue

            entry = float(row.get('openPrice') or row.get('avgPrice') or p.entry_price)
            p.entry_price = entry
            p.stop_price = float(row.get('stopLossPrice') or p.stop_price)
            p.target_price = float(row.get('stopProfitPrice') or p.target_price)

            qty = float(row.get('baseSize') or 0)
            if not qty:
                unit = int(row.get('quantityUnit') or 0)
                q = float(row.get('quantity') or 0)
                qty = q / max(entry, 1e-12) if unit == 0 else q
            if qty > 0:
                p.remaining_quantity = qty
                p.quantity = max(p.quantity, qty)
            self._persist(p)

        for pid, row in exchange.items():
            if pid in local_open:
                continue
            entry = float(row.get('openPrice') or row.get('avgPrice') or 0)
            if entry <= 0:
                continue
            direction = Direction.LONG if str(row.get('direction', '')).lower() == 'long' else Direction.SHORT
            unit = int(row.get('quantityUnit') or 0)
            q = float(row.get('quantity') or 0)
            qty = float(row.get('baseSize') or 0)
            if not qty:
                qty = q / max(entry, 1e-12) if unit == 0 else q
            recovered = Position(
                position_id=pid,
                decision_id='EXCHANGE_RECOVERED',
                symbol=str(row.get('instrument') or ''),
                direction=direction,
                quantity=qty,
                entry_price=entry,
                stop_price=float(row.get('stopLossPrice') or entry),
                target_price=float(row.get('stopProfitPrice') or entry),
                remaining_quantity=qty,
                opened_at=int(row.get('createdDate') or row.get('updatedDate') or timestamp),
                entry_fee=float(row.get('fee') or 0),
            )
            self.add(recovered)

    def close_or_reduce(self, p, action, price, timestamp):
        qty = p.remaining_quantity or p.quantity
        if action == 'TP1' and not p.tp1_hit:
            close_qty = qty * 0.5
            gross = self._pnl(p, price, close_qty)
            p.realized_pnl += gross
            p.remaining_quantity = qty - close_qty
            p.tp1_hit = True
            p.stop_price = p.entry_price
            p.stop_moved_to_breakeven = True
            p.unrealized_pnl = self._pnl(p, price, p.remaining_quantity)
            if self.on_realized:
                self.on_realized(p, gross, close_qty, price)
            self._audit('TP1', p, price, close_qty, gross)
        elif action in ('TP2', 'SL'):
            gross = self._pnl(p, price, qty)
            p.realized_pnl += gross
            p.exit_price = price
            p.exit_reason = action
            p.closed_at = timestamp
            p.status = 'CLOSED'
            p.remaining_quantity = 0
            p.unrealized_pnl = 0.0
            if self.on_realized:
                self.on_realized(p, gross, qty, price)
            self._audit(action, p, price, qty, gross)
        self._persist(p)

    @staticmethod
    def _pnl(p, price, qty):
        return (
            (price - p.entry_price) * qty
            if p.direction == Direction.LONG
            else (p.entry_price - price) * qty
        )

    def _persist(self, p):
        if self.db:
            document = dict(p.__dict__)
            if self.owner_user_id:
                document['user_id'] = self.owner_user_id
            self.db.upsert('positions', {'position_id': p.position_id}, document)

    def _audit(self, event, p, price, qty, gross):
        if self.audit:
            self.audit.event(
                event, p.decision_id, position_id=p.position_id,
                price=price, quantity=qty, gross_pnl=gross
            )
