from dataclasses import dataclass
from app.models.market import Candle
from app.models.regime import RegimeResult
from app.models.trading import TradeIntent
from app.models.enums import Direction, Strategy
from app.position.tp_sl import TpSlEngine


@dataclass(frozen=True)
class BreakoutConfig:
    min_level_strength: float = 58.0
    min_breakout_quality: float = 60.0
    min_retest_quality: float = 58.0
    min_confirmation: float = 58.0
    min_rr: float = 1.8
    zone_atr: float = 0.20
    stop_buffer_atr: float = 0.12


class BreakoutRetestStrategy:
    def __init__(self, cfg=None):
        self.cfg = cfg or BreakoutConfig()

    def evaluate(self, regime: RegimeResult, candles: list[Candle],
                 decision_id: str, symbol: str, timeframe: str,
                 current_price: float | None = None):
        if regime.hard_block or not regime.breakout_allowed or len(candles) < 40:
            return None

        price = current_price or candles[-1].close
        atr = self._atr(candles, 14)
        if atr <= 0:
            return None

        direction = regime.direction
        if direction not in (Direction.BULLISH, Direction.BEARISH):
            return None

        level = self._level(candles, atr, direction)
        if not level:
            return None

        zone = atr * self.cfg.zone_atr
        breakout = self._breakout(candles, level, atr, zone, direction)
        if breakout < self.cfg.min_breakout_quality:
            return None

        retest = self._retest(candles, level, atr, zone, direction)
        if retest < self.cfg.min_retest_quality:
            return None

        confirmation = self._confirmation(candles, atr, direction)
        if confirmation < self.cfg.min_confirmation:
            return None

        if direction == Direction.BULLISH:
            structural_stop = min(
                min(x.low for x in candles[-4:]), level - zone
            )
        else:
            structural_stop = max(
                max(x.high for x in candles[-4:]), level + zone
            )

        # The central TP/SL engine owns the ATR buffer. Do not buffer here too.
        plan = TpSlEngine(
            rr1=1.0, rr2=2.0, buffer_atr=self.cfg.stop_buffer_atr
        ).build(
            Strategy.BREAKOUT_RETEST,
            direction,
            price,
            structural_stop,
            atr,
            min_rr2=self.cfg.min_rr,
        )

        quality = min(
            100.0,
            .35 * breakout + .30 * retest + .25 * confirmation
            + .10 * regime.confidence,
        )
        return TradeIntent(
            decision_id=decision_id,
            symbol=symbol,
            strategy=Strategy.BREAKOUT_RETEST,
            direction=direction,
            entry_price=price,
            stop_price=plan.stop_price,
            target_price=plan.tp2_price,
            quality=quality,
            risk_multiplier=regime.risk_multiplier,
            timeframe=timeframe,
            reasons=(
                "structural_level",
                "breakout_acceptance",
                "valid_retest",
                "entry_confirmation",
            ),
            metadata={
                "level": level,
                "breakout_quality": breakout,
                "retest_quality": retest,
                "confirmation": confirmation,
                "tp1_price": plan.tp1_price,
                "tp2_price": plan.tp2_price,
                "risk_distance": plan.risk_distance,
            },
        )

    def _atr(self, candles, n):
        return sum(x.high - x.low for x in candles[-n:]) / n

    def _level(self, candles, atr, direction):
        candidates = []
        for i in range(5, len(candles) - 5):
            if direction == Direction.BULLISH:
                is_pivot = candles[i].high >= max(x.high for x in candles[i - 2:i + 3])
                level = candles[i].high
                tests = sum(
                    1 for x in candles[i + 1:]
                    if abs(x.high - level) <= atr * .15
                )
            else:
                is_pivot = candles[i].low <= min(x.low for x in candles[i - 2:i + 3])
                level = candles[i].low
                tests = sum(
                    1 for x in candles[i + 1:]
                    if abs(x.low - level) <= atr * .15
                )

            if not is_pivot:
                continue

            recency = max(0, 1 - (len(candles) - i) / len(candles))
            strength = min(100, 45 + min(tests, 4) * 9 + recency * 18)
            if strength >= self.cfg.min_level_strength:
                candidates.append((strength, level))
        return max(candidates)[1] if candidates else None

    def _breakout(self, candles, level, atr, zone, direction):
        x = candles[-1]
        p = candles[-2]
        if direction == Direction.BULLISH:
            close_score = max(0, min(100, (x.close - level) / max(atr, 1e-9) * 100))
            displacement = max(0, min(100, (x.close - p.close) / max(atr, 1e-9) * 70))
            continuation = max(0, min(100, (x.close - candles[-4].close) / max(atr, 1e-9) * 45))
            rejection = 35 if x.low < level - zone and x.close < level + zone * .2 else 0
        else:
            close_score = max(0, min(100, (level - x.close) / max(atr, 1e-9) * 100))
            displacement = max(0, min(100, (p.close - x.close) / max(atr, 1e-9) * 70))
            continuation = max(0, min(100, (candles[-4].close - x.close) / max(atr, 1e-9) * 45))
            rejection = 35 if x.high > level + zone and x.close > level - zone * .2 else 0

        return max(
            0,
            min(100, .38 * close_score + .32 * displacement
                + .20 * continuation + .10 * 65 - rejection),
        )

    def _retest(self, candles, level, atr, zone, direction):
        touched = any(
            x.low <= level + zone and x.high >= level - zone
            for x in candles[-8:-1]
        )
        if not touched:
            return 0

        last = candles[-1]
        if direction == Direction.BULLISH:
            hold = 100 if last.close > level else 0
            deepest = min(x.low for x in candles[-8:-1])
            depth = max(0, min(100, 100 - abs(deepest - level) / max(atr, 1e-9) * 80))
            reaction = max(0, min(100, (last.close - level) / max(atr, 1e-9) * 100))
        else:
            hold = 100 if last.close < level else 0
            deepest = max(x.high for x in candles[-8:-1])
            depth = max(0, min(100, 100 - abs(deepest - level) / max(atr, 1e-9) * 80))
            reaction = max(0, min(100, (level - last.close) / max(atr, 1e-9) * 100))

        return .45 * hold + .30 * depth + .25 * reaction

    def _confirmation(self, candles, atr, direction):
        if direction == Direction.BULLISH:
            displacement = max(0, min(100, (candles[-1].close - candles[-3].close) / max(atr, 1e-9) * 60))
            internal = 100 if candles[-1].close > candles[-2].high else 55
        else:
            displacement = max(0, min(100, (candles[-3].close - candles[-1].close) / max(atr, 1e-9) * 60))
            internal = 100 if candles[-1].close < candles[-2].low else 55
        return .55 * displacement + .45 * internal
