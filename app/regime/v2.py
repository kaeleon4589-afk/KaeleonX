from __future__ import annotations

from dataclasses import dataclass

from app.models.enums import Direction, RegimeState
from app.models.regime import RegimeResult
from app.strategy.source_math import extract, atr, adx, relative_volume
from app.strategy.v2_common import choppiness, efficiency_ratio, env_float, env_int, frames, trend_alignment

TREND = "TREND"
RANGE = "RANGE"
SHOCK = "SHOCK"
DEAD = "DEAD"
TRANSITION = "TRANSITION"


@dataclass
class _State:
    active: str = TRANSITION
    pending: str = ""
    pending_count: int = 0
    last_bar: int | None = None
    bars_active: int = 0


def _raw(snapshot) -> tuple[str, str, float, dict]:
    tf = frames(snapshot, getattr(snapshot, "candles", None))
    c5 = list(tf.get("5m") or [])
    c15 = list(tf.get("15m") or [])
    c1h = list(tf.get("1h") or [])
    if len(c5) < 60 or len(c15) < 60 or len(c1h) < 60:
        return TRANSITION, "neutral", 0.0, {"reason": "insufficient_multitimeframe_context"}

    _, h5, l5, close5, v5 = extract(c5)
    price = float(close5[-1])
    atr5 = float(atr(h5, l5, close5, 14))
    atr_pct = atr5 / max(price, 1e-12)
    adx5 = float(adx(h5, l5, close5, 14))
    chop = choppiness(h5, l5, close5, 14)
    eff = efficiency_ratio(close5, 20)
    rvol = float(relative_volume(v5, len(v5) - 1, 20)) if v5 else 0.0

    d15, a15, m15 = trend_alignment(c15, slow=min(200, max(50, len(c15) - 5)))
    d1h, a1h, m1h = trend_alignment(c1h, slow=min(200, max(50, len(c1h) - 5)))
    d5, a5, m5 = trend_alignment(c5, slow=min(200, max(50, len(c5) - 5)))

    aligned = d15 == d1h and d15 in {"long", "short"}
    direction = d15 if aligned else (d1h if d1h == d5 and d1h in {"long", "short"} else "neutral")

    last = c5[-1]
    last_range_atr = (float(last.high) - float(last.low)) / max(atr5, 1e-12)
    if len(close5) >= 4:
        move3_atr = abs(float(close5[-1]) - float(close5[-4])) / max(atr5, 1e-12)
    else:
        move3_atr = 0.0
    spread_bps = 0.0
    try:
        bid = float(getattr(snapshot, "bid", 0.0) or 0.0)
        ask = float(getattr(snapshot, "ask", 0.0) or 0.0)
        spread_bps = ((ask - bid) / bid * 10000.0) if bid > 0 and ask >= bid else 0.0
    except (TypeError, ValueError):
        spread_bps = 0.0

    shock_range = env_float("V2_REGIME_SHOCK_CANDLE_ATR", 2.40, 1.2, 6.0)
    shock_move = env_float("V2_REGIME_SHOCK_MOVE3_ATR", 3.20, 1.5, 8.0)
    shock_spread = env_float("V2_REGIME_SHOCK_SPREAD_BPS", 35.0, 5.0, 200.0)
    dead_atr = env_float("V2_REGIME_DEAD_ATR_PCT", 0.0012, 0.0001, 0.01)
    dead_rvol = env_float("V2_REGIME_DEAD_RVOL", 0.70, 0.1, 2.0)

    if last_range_atr >= shock_range or move3_atr >= shock_move or spread_bps >= shock_spread:
        candidate = SHOCK
        confidence = min(0.99, 0.70 + 0.08 * max(last_range_atr / shock_range - 1.0, move3_atr / shock_move - 1.0, 0.0))
    elif atr_pct <= dead_atr and rvol <= dead_rvol:
        candidate = DEAD
        confidence = 0.78
    else:
        trend_score = 0.0
        trend_score += 30.0 if aligned else (18.0 if direction != "neutral" else 0.0)
        trend_score += 15.0 * max(a15, a1h)
        trend_score += 15.0 * min(1.0, adx5 / 25.0)
        trend_score += 15.0 * min(1.0, eff / 0.42)
        trend_score += 12.0 * min(1.0, max(0.0, (60.0 - chop) / 18.0))
        trend_score += 13.0 * a5

        range_score = 0.0
        range_score += 22.0 * min(1.0, max(0.0, (25.0 - adx5) / 12.0))
        range_score += 22.0 * min(1.0, max(0.0, (chop - 45.0) / 18.0))
        range_score += 18.0 * min(1.0, max(0.0, (0.48 - eff) / 0.30))
        range_score += 20.0 if not aligned else 4.0
        range_score += 18.0 * min(1.0, max(0.0, 1.0 - max(a15, a1h)))

        trend_min = env_float("V2_REGIME_TREND_SCORE_MIN", 61.0, 45.0, 90.0)
        range_min = env_float("V2_REGIME_RANGE_SCORE_MIN", 58.0, 40.0, 90.0)
        # EMA ordering alone is not enough. In a flat market tiny numerical EMA
        # differences can produce a perfectly ordered stack while price remains
        # highly choppy and directionally inefficient. Require actual trend
        # behaviour before TREND can win the router.
        trend_behavior_ok = (
            (adx5 >= 17.0 and eff >= 0.20 and chop <= 62.0)
            or (adx5 >= 24.0 and eff >= 0.15 and chop <= 68.0)
        )
        if direction != "neutral" and trend_behavior_ok and trend_score >= trend_min and trend_score >= range_score + 4.0:
            candidate = TREND
            confidence = min(0.96, 0.50 + trend_score / 200.0)
        elif range_score >= range_min and (range_score >= trend_score - 2.0 or not trend_behavior_ok):
            candidate = RANGE
            confidence = min(0.94, 0.48 + range_score / 210.0)
            direction = "neutral"
        else:
            candidate = TRANSITION
            confidence = max(0.20, min(0.65, max(trend_score, range_score) / 120.0))

    diag = {
        "candidate": candidate,
        "direction": direction,
        "confidence": confidence,
        "atr": atr5,
        "atr_pct": atr_pct,
        "adx5": adx5,
        "choppiness": chop,
        "efficiency": eff,
        "rvol": rvol,
        "last_range_atr": last_range_atr,
        "move3_atr": move3_atr,
        "spread_bps": spread_bps,
        "alignment_5m": a5,
        "alignment_15m": a15,
        "alignment_1h": a1h,
        "direction_5m": d5,
        "direction_15m": d15,
        "direction_1h": d1h,
        "trend_behavior_ok": locals().get("trend_behavior_ok", False),
        "trend_score": locals().get("trend_score"),
        "range_score": locals().get("range_score"),
        "ema_5m": m5,
        "ema_15m": m15,
        "ema_1h": m1h,
    }
    return candidate, direction, float(confidence), diag


class RegimeEngineV2:
    """Four-state market router for the rebuilt KAELEON engine.

    TREND -> BREAKOUT_RETEST_V2
    RANGE -> LIQUIDITY_SWEEP_V2
    SHOCK/DEAD/TRANSITION -> NO_TRADE
    """

    def __init__(self):
        self._states: dict[str, _State] = {}
        self.last_metadata: dict = {}

    def evaluate_snapshot(self, snapshot):
        candidate, raw_direction, confidence, diag = _raw(snapshot)
        symbol = str(getattr(snapshot, "symbol", "UNKNOWN"))
        stamp = None
        candles = list(getattr(snapshot, "candles", []) or [])
        if candles:
            stamp = int(candles[-1].timestamp)

        state = self._states.setdefault(symbol, _State())
        first_observation = state.last_bar is None
        if stamp is not None and state.last_bar != stamp:
            state.last_bar = stamp
            if first_observation:
                state.active = candidate
                state.pending = ""
                state.pending_count = 0
                state.bars_active = 1
            elif candidate == SHOCK:
                state.active = SHOCK
                state.pending = ""
                state.pending_count = 0
                state.bars_active = 1
            elif candidate == state.active:
                state.bars_active += 1
                state.pending = ""
                state.pending_count = 0
            else:
                state.bars_active += 1
                if state.pending == candidate:
                    state.pending_count += 1
                else:
                    state.pending = candidate
                    state.pending_count = 1
                confirm = env_int("V2_REGIME_CONFIRM_BARS", 2, 1, 6)
                if state.active in {SHOCK, DEAD, TRANSITION} and candidate in {TREND, RANGE}:
                    required = confirm
                elif candidate in {DEAD, TRANSITION}:
                    required = max(2, confirm)
                else:
                    required = confirm
                if state.pending_count >= required:
                    state.active = candidate
                    state.pending = ""
                    state.pending_count = 0
                    state.bars_active = 1

        active = state.active

        if active == TREND:
            direction = Direction.LONG if raw_direction == "long" else (Direction.SHORT if raw_direction == "short" else Direction.NEUTRAL)
            breakout_allowed = direction in {Direction.LONG, Direction.SHORT}
            sweep_allowed = False
            risk_multiplier = 1.0
            rs = RegimeState.TRENDING
            preferred = "BREAKOUT_RETEST"
            hard_block = not breakout_allowed
        elif active == RANGE:
            direction = Direction.NEUTRAL
            breakout_allowed = False
            sweep_allowed = True
            risk_multiplier = 0.80
            rs = RegimeState.RANGING
            preferred = "LIQUIDITY_SWEEP"
            hard_block = False
        elif active == SHOCK:
            direction = Direction.NEUTRAL
            breakout_allowed = sweep_allowed = False
            risk_multiplier = 0.0
            rs = RegimeState.EXTREME
            preferred = None
            hard_block = True
        elif active == DEAD:
            direction = Direction.NEUTRAL
            breakout_allowed = sweep_allowed = False
            risk_multiplier = 0.0
            rs = RegimeState.CHOPPY
            preferred = None
            hard_block = True
        else:
            direction = Direction.NEUTRAL
            breakout_allowed = sweep_allowed = False
            risk_multiplier = 0.0
            rs = RegimeState.TRANSITION
            preferred = None
            hard_block = True

        self.last_metadata = {
            **diag,
            "candidate": candidate,
            "active": active,
            "pending": state.pending,
            "pending_count": state.pending_count,
            "bars_in_active": state.bars_active,
            "state": {
                "active": active,
                "candidate": candidate,
                "pending": state.pending,
                "pending_count": state.pending_count,
                "bars": state.bars_active,
            },
            "engine": "regime_v2",
        }
        return RegimeResult(
            global_state=rs,
            asset_state=rs,
            direction=direction,
            quality="HIGH" if confidence >= 0.75 else ("MEDIUM" if confidence >= 0.55 else "LOW"),
            core_score=confidence * 100.0,
            breakout_allowed=breakout_allowed,
            sweep_allowed=sweep_allowed,
            preferred=preferred,
            risk_multiplier=risk_multiplier,
            trap_risk=0.0 if active in {TREND, RANGE} else 100.0,
            confidence=confidence * 100.0,
            hard_block=hard_block,
            reasons=(f"regime={active}", f"candidate={candidate}", f"confidence={confidence:.2f}"),
        )

    def evaluate(self, *signals):
        if len(signals) == 1 and hasattr(signals[0], "candles"):
            return self.evaluate_snapshot(signals[0])
        return RegimeResult(
            RegimeState.TRANSITION, RegimeState.TRANSITION, Direction.NEUTRAL,
            "LOW", 0.0, False, False, None, 0.0, 100.0, 0.0, True,
            ("legacy_signals_unsupported",),
        )


def evaluate_snapshot_once(snapshot):
    engine = RegimeEngineV2()
    result = engine.evaluate_snapshot(snapshot)
    return result, dict(engine.last_metadata)
