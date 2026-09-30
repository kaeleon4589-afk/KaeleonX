"""Explicit, reversible discovery policy; execution guards remain independent."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PrearmPolicy:
    name: str
    range_enabled: bool
    mtf_require_both_opposed: bool
    breakout_lookback: int
    sweep_lookback: int
    breakout_min_body: float
    breakout_close_long: float
    breakout_min_rvol: float
    breakout_max_extension: float
    sweep_proximity_atr: float

    def mtf_conflict(self, direction: str, bias_1h: str, bias_15m: str) -> bool:
        opposite = "short" if direction == "long" else "long"
        if self.mtf_require_both_opposed:
            return bias_1h == opposite and bias_15m == opposite
        return any(bias == opposite for bias in (bias_1h, bias_15m))


STRICT = PrearmPolicy("strict", False, False, 20, 34, .30, .64, .95, .60, .45)
RECOVERY = PrearmPolicy("recovery", True, True, 12, 12, .22, .58, .70, .85, .80)


def prearm_policy() -> PrearmPolicy:
    # Read dynamically so tests and process configuration use the same path.
    # Invalid explicit values fail closed to the previous discovery policy.
    name = os.getenv("TRADE_PREARM_PROFILE", "recovery").strip().lower()
    return RECOVERY if name == "recovery" else STRICT
