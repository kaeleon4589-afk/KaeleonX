from app.models.enums import Direction


class ExitEngine:
    def evaluate(self, p, price):
        stop = float(getattr(p, 'stop_price', 0.0) or 0.0)
        target = float(getattr(p, 'tp2_price', None) or getattr(p, 'target_price', 0.0) or 0.0)
        tp1 = float(getattr(p, 'tp1_price', 0.0) or 0.0)
        if p.direction == Direction.LONG:
            if stop > 0 and price <= stop:
                return 'SL'
            if tp1 > 0 and not p.tp1_hit and price >= tp1:
                return 'TP1'
            if (p.tp1_hit or tp1 <= 0) and target > 0 and price >= target:
                return 'TP2'
        else:
            if stop > 0 and price >= stop:
                return 'SL'
            if tp1 > 0 and not p.tp1_hit and price <= tp1:
                return 'TP1'
            if (p.tp1_hit or tp1 <= 0) and target > 0 and price <= target:
                return 'TP2'
        return None
