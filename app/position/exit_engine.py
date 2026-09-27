from app.models.enums import Direction
class ExitEngine:
    def evaluate(self,p,price):
        if p.direction==Direction.LONG:
            if price<=p.stop_price:return 'SL'
            if p.tp1_price and not p.tp1_hit and price>=p.tp1_price:return 'TP1'
            if (p.tp1_hit or not p.tp1_price) and (p.tp2_price or p.target_price) and price>=float(p.tp2_price or p.target_price):return 'TP2'
        else:
            if price>=p.stop_price:return 'SL'
            if p.tp1_price and not p.tp1_hit and price<=p.tp1_price:return 'TP1'
            if (p.tp1_hit or not p.tp1_price) and (p.tp2_price or p.target_price) and price<=float(p.tp2_price or p.target_price):return 'TP2'
        return None
