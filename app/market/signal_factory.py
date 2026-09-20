from __future__ import annotations
from app.models.enums import Direction
from app.models.regime import EngineSignal


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, float(x)))


class SignalFactory:
    """Builds normalized regime-engine signals from live candles/orderbook."""
    def build(self, snapshot):
        c = snapshot.candles
        if len(c) < 30 or not snapshot.data_complete or not snapshot.orderbook_valid:
            blocked = EngineSignal(Direction.NEUTRAL, 0, 0, "", True)
            return blocked, blocked, blocked, blocked, blocked

        closes = [x.close for x in c]
        ranges = [max(0.0, x.high - x.low) for x in c]
        atr = sum(ranges[-14:]) / 14
        if atr <= 0:
            blocked = EngineSignal(Direction.NEUTRAL, 0, 0, "", True)
            return blocked, blocked, blocked, blocked, blocked

        # Structure: slope + recent swing consistency.
        fast = sum(closes[-5:]) / 5
        slow = sum(closes[-20:]) / 20
        slope = (fast - slow) / atr
        direction = Direction.BULLISH if slope > .35 else Direction.BEARISH if slope < -.35 else Direction.NEUTRAL
        consistency = sum(1 for i in range(-5, 0) if (closes[i] > closes[i-1]) == (slope > 0)) / 5
        structure_strength = _clamp(abs(slope) * 45 + consistency * 45)
        structure_state = "TRENDING" if structure_strength >= 55 and direction != Direction.NEUTRAL else "RANGING"
        structure = EngineSignal(direction, structure_strength, structure_strength, structure_state)

        # Momentum/displacement.
        move = (closes[-1] - closes[-4]) / atr
        mom_dir = Direction.BULLISH if move > .25 else Direction.BEARISH if move < -.25 else Direction.NEUTRAL
        momentum = EngineSignal(mom_dir, _clamp(abs(move) * 55), _clamp(abs(move) * 60), "EXPANDING" if abs(move) > .8 else "NORMAL")

        # Volatility.
        recent_atr = sum(ranges[-5:]) / 5
        base_atr = sum(ranges[-30:]) / 30
        ratio = recent_atr / max(base_atr, 1e-12)
        vol_state = "EXTREME" if ratio > 2.5 else "EXPANSION" if ratio > 1.35 else "COMPRESSION" if ratio < .7 else "NORMAL"
        volatility = EngineSignal(direction, _clamp(50 + abs(ratio - 1) * 60), 75, vol_state, False, {"atr": atr, "ratio": ratio})

        # Liquidity/orderbook: spread + top-of-book imbalance.
        bid_size = sum(float(x[1]) for x in snapshot.bids[:10]) if snapshot.bids else 0
        ask_size = sum(float(x[1]) for x in snapshot.asks[:10]) if snapshot.asks else 0
        imb = (bid_size - ask_size) / max(bid_size + ask_size, 1e-12)
        liq_dir = Direction.BULLISH if imb > .08 else Direction.BEARISH if imb < -.08 else Direction.NEUTRAL
        spread = ((snapshot.ask-snapshot.bid)/snapshot.bid) if snapshot.bid and snapshot.ask and snapshot.bid > 0 else 0
        spread_penalty = max(0.0, min(50.0, spread * 10000 - 3))
        liquidity = EngineSignal(liq_dir, _clamp(55 + abs(imb) * 45 - spread_penalty), 75, "NORMAL", spread_penalty > 30, {"imbalance": imb, "spread": spread})

        # Breadth is neutral for a single-symbol loop until the basket engine is wired.
        breadth = EngineSignal(Direction.NEUTRAL, 50, 60, "NEUTRAL", False, {"scope": "asset_only"})
        return structure, momentum, volatility, liquidity, breadth
