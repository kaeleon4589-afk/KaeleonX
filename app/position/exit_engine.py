from app.models.enums import Direction
class ExitEngine:
    def evaluate(self,p,price):
        if p.direction==Direction.LONG:
            if price<=p.stop_price: return 'SL'
            if not p.tp1_hit and p.tp1_price and price>=p.tp1_price: return 'TP1'
            if p.tp1_hit and p.tp2_price and price>=p.tp2_price: return 'TP2'
        else:
            if price>=p.stop_price: return 'SL'
            if not p.tp1_hit and p.tp1_price and price<=p.tp1_price: return 'TP1'
            if p.tp1_hit and p.tp2_price and price<=p.tp2_price: return 'TP2'
        return None
