from uuid import uuid4
import math
from app.models.trading import Position
from app.models.enums import Direction

class PaperExecutionEngine:
    mode='paper'
    def __init__(self,audit=None,fee_rate=.0006,slippage_bps=2.0,max_spread_bps=30,initial_equity=10000,leverage=10):
        self.leverage=leverage
        self.positions={}; self.orders={}; self.audit=audit; self.fee_rate=fee_rate; self.slippage_bps=slippage_bps; self.max_spread_bps=max_spread_bps
        self.equity=float(initial_equity); self.realized_pnl=0.0; self.reserved_margin=0.0

    def submit(self,intent,quantity,market):
        # Order-book data can be temporarily missing for individual CoinW symbols.
        # Never let a None/string quote crash the whole user runtime.
        try:
            bid=float(market.get('bid'))
            ask=float(market.get('ask'))
        except (TypeError, ValueError):
            return {'accepted':False,'filled':False,'reason':'market_unavailable'}
        if not math.isfinite(bid) or not math.isfinite(ask) or bid<=0 or ask<=0 or bid>ask:
            return {'accepted':False,'filled':False,'reason':'market_unavailable'}
        spread=(ask-bid)/bid*10000
        if spread>self.max_spread_bps: return {'accepted':False,'filled':False,'reason':'spread_too_wide'}
        raw=ask if intent.direction==Direction.LONG else bid
        fill=raw*(1+self.slippage_bps/10000) if intent.direction==Direction.LONG else raw*(1-self.slippage_bps/10000)
        if not math.isfinite(quantity) or quantity <= 0:
            return {'accepted':False,'filled':False,'reason':'invalid_quantity'}
        if not ((intent.direction == Direction.LONG and intent.stop_price < fill < intent.target_price)
                or (intent.direction == Direction.SHORT and intent.target_price < fill < intent.stop_price)):
            return {'accepted':False,'filled':False,'reason':'fill_outside_trade_geometry'}
        if quantity / self.leverage + quantity * self.fee_rate > self.equity:
            return {'accepted':False,'filled':False,'reason':'insufficient_margin'}
        # RiskManager returns quote-currency notional (same unit as CoinW quantityUnit=0).
        base_quantity=quantity/max(fill,1e-12)
        fee=fill*base_quantity*self.fee_rate
        pid='PAPER-'+uuid4().hex[:16]
        p=Position(pid,intent.decision_id,intent.symbol,intent.direction,base_quantity,fill,intent.stop_price,intent.target_price,entry_fee=fee,opened_at=market.get('ts'))
        p.tp1_price=(float(intent.metadata['tp1_price']) if intent.metadata.get('partial_tp_enabled') and intent.metadata.get('tp1_price') else None)
        p.tp2_price=float(intent.metadata.get('tp2_price', intent.target_price))
        p.remaining_quantity=base_quantity
        p.leverage=self.leverage
        p.current_price=fill
        self.positions[pid]=p
        self.equity -= fee
        self.orders[pid]={'order_id':pid,'status':'FILLED','fill_price':fill,'fee':fee,'decision_id':intent.decision_id}
        if self.audit: self.audit.event('PAPER_FILL',intent.decision_id,position_id=pid,quantity=base_quantity,notional=quantity,price=fill,fee=fee)
        return {'accepted':True,'filled':True,'position_id':pid,'fill_price':fill,'fee':fee,'mode':'demo','position':p}

    async def get_equity(self, max_age_seconds=0.0):
        return float(self.equity)

    def on_realized(self, position, gross_pnl, quantity, price):
        exit_fee=price*quantity*self.fee_rate
        position.exit_fee += exit_fee
        net=gross_pnl-exit_fee
        self.realized_pnl += net
        self.equity += net

    def mark_to_market(self, position_manager, symbol, price, ts):
        position_manager.mark(symbol,price,ts)

    def sync(self): return list(self.positions.values())
