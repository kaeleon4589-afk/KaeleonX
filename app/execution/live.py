import time


class LiveExecutionEngine:
    mode = "live"

    def __init__(self, coinw_executor, leverage=10):
        self.coinw = coinw_executor
        self.leverage = leverage
        self._equity_cache = 0.0
        self._equity_cache_ts = 0.0
        self._position_cache = {}
        self._position_cache_ts = {}

    async def submit(self, intent, quantity, market=None):
        return await self.coinw.submit_intent(
            intent, quantity, leverage=self.leverage
        )

    async def get_equity(self, max_age_seconds=5.0):
        now = time.time()
        if now - self._equity_cache_ts > max_age_seconds or self._equity_cache <= 0:
            self._equity_cache = await self.coinw.equity()
            self._equity_cache_ts = now
        return self._equity_cache

    async def sync_symbol(self, symbol, max_age_seconds=2.0):
        now = time.time()
        if now - self._position_cache_ts.get(symbol, 0) > max_age_seconds:
            self._position_cache[symbol] = await self.coinw.current_position_rows(symbol)
            self._position_cache_ts[symbol] = now
        return list(self._position_cache.get(symbol, []))

    async def pending_order_status(self, order_id):
        return await self.coinw.pending_order_status(order_id)

    async def sync(self, instruments):
        return await self.coinw.sync_positions(instruments)

    async def settlements(self, symbol, position_ids):
        return await self.coinw.settlements(symbol, position_ids)

    async def ensure_protection(self, position):
        if hasattr(self.coinw, "ensure_protection"):
            return await self.coinw.ensure_protection(position)
        await self.coinw.orders.set_tpsl(
            position.position_id, self.coinw._instrument(position.symbol),
            stop_loss=position.stop_price, take_profit=position.target_price,
        )
        return position
