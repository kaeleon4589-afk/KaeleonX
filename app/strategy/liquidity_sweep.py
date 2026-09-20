from dataclasses import dataclass
from app.models.market import Candle
from app.models.regime import RegimeResult
from app.models.trading import TradeIntent
from app.models.enums import Direction, Strategy
from app.position.tp_sl import TpSlEngine


@dataclass(frozen=True)
class SweepConfig:
    min_quality: float = 60.0
    min_rr: float = 1.8
    stop_buffer_atr: float = .12


class LiquiditySweepStrategy:
    def __init__(self, cfg=None):
        self.cfg = cfg or SweepConfig()

    def evaluate(self, regime: RegimeResult, candles: list[Candle],
                 decision_id: str, symbol: str, timeframe: str,
                 current_price: float | None = None):
        if not regime.sweep_allowed or regime.hard_block or len(candles) < 30:
            return None
        atr = sum(x.high - x.low for x in candles[-14:]) / 14
        p = current_price or candles[-1].close
        x = candles[-1]
        hi = max(z.high for z in candles[-12:-2])
        lo = min(z.low for z in candles[-12:-2])

        if x.low < lo and x.close > lo:
            q = self._quality(
                (lo - x.low) / max(atr, 1e-9),
                (x.close - lo) / max(atr, 1e-9),
                (x.close - candles[-3].close) / max(atr, 1e-9),
            )
            if q >= self.cfg.min_quality:
                plan = TpSlEngine(
                    rr1=1.0, rr2=2.0, buffer_atr=self.cfg.stop_buffer_atr
                ).build(
                    Strategy.LIQUIDITY_SWEEP,
                    Direction.LONG,
                    p,
                    x.low,  # central engine applies the ATR buffer
                    atr,
                    min_rr2=self.cfg.min_rr,
                )
                return TradeIntent(
                    decision_id, symbol, Strategy.LIQUIDITY_SWEEP,
                    Direction.LONG, p, plan.stop_price, plan.tp2_price,
                    q, regime.risk_multiplier, timeframe,
                    ("liquidity_taken", "reclaimed", "bullish_displacement"),
                    {
                        "quality": q,
                        "liquidity_level": lo,
                        "tp1_price": plan.tp1_price,
                        "tp2_price": plan.tp2_price,
                        "risk_distance": plan.risk_distance,
                    },
                )

        if x.high > hi and x.close < hi:
            q = self._quality(
                (x.high - hi) / max(atr, 1e-9),
                (hi - x.close) / max(atr, 1e-9),
                (candles[-3].close - x.close) / max(atr, 1e-9),
            )
            if q >= self.cfg.min_quality:
                plan = TpSlEngine(
                    rr1=1.0, rr2=2.0, buffer_atr=self.cfg.stop_buffer_atr
                ).build(
                    Strategy.LIQUIDITY_SWEEP,
                    Direction.SHORT,
                    p,
                    x.high,  # central engine applies the ATR buffer
                    atr,
                    min_rr2=self.cfg.min_rr,
                )
                return TradeIntent(
                    decision_id, symbol, Strategy.LIQUIDITY_SWEEP,
                    Direction.SHORT, p, plan.stop_price, plan.tp2_price,
                    q, regime.risk_multiplier, timeframe,
                    ("liquidity_taken", "reclaimed", "bearish_displacement"),
                    {
                        "quality": q,
                        "liquidity_level": hi,
                        "tp1_price": plan.tp1_price,
                        "tp2_price": plan.tp2_price,
                        "risk_distance": plan.risk_distance,
                    },
                )
        return None

    def _quality(self, penetration, reclaim, displacement):
        return min(
            100,
            max(
                0,
                .35 * min(1.5, penetration) * 60
                + .35 * min(1.5, reclaim) * 60
                + .30 * min(1.5, displacement) * 60,
            ),
        )
