from __future__ import annotations

import os
from dataclasses import replace, is_dataclass
from types import SimpleNamespace

from app.strategy.breakout_retest import BreakoutRetestStrategy
from app.strategy.armed_entry import ArmedEntryEngine
from app.strategy.liquidity_sweep import LiquiditySweepStrategy


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() not in {'0', 'false', 'no', 'off', ''}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


class StrategyRouter:
    """KAELEON strategy router with v6 armed-entry support.

    Production v6 uses regime as context: TREND_CONTINUATION primarily arms
    breakout/retest setups, while VOLATILE_SWEEP and RANGE can arm high-quality
    liquidity sweeps. UNKNOWN remains blocked. The legacy immediate-entry router
    is kept intact as an explicit rollback/compatibility path.
    """

    def __init__(
        self,
        armed_ttl_seconds: float | None = None,
        armed_chase_tolerance_atr: float | None = None,
        armed_trigger_close_tolerance_atr: float | None = None,
        armed_consumed_ttl_seconds: float | None = None,
    ):
        self.breakout = BreakoutRetestStrategy()
        self.sweep = LiquiditySweepStrategy()
        ttl = _env_float("TRADE_ARMED_SETUP_TTL_SECONDS", 600.0) if armed_ttl_seconds is None else armed_ttl_seconds
        chase_tolerance = (
            _env_float("TRADE_ARMED_CHASE_TOLERANCE_ATR", 0.15)
            if armed_chase_tolerance_atr is None else armed_chase_tolerance_atr
        )
        close_tolerance = (
            _env_float("TRADE_ARMED_TRIGGER_CLOSE_TOLERANCE_ATR", 0.08)
            if armed_trigger_close_tolerance_atr is None else armed_trigger_close_tolerance_atr
        )
        consumed_ttl = (
            _env_float("TRADE_ARMED_CONSUMED_TTL_SECONDS", 3600.0)
            if armed_consumed_ttl_seconds is None else armed_consumed_ttl_seconds
        )
        self.armed = ArmedEntryEngine(
            ttl_seconds=ttl,
            chase_tolerance_atr=chase_tolerance,
            trigger_close_tolerance_atr=close_tolerance,
            consumed_ttl_seconds=consumed_ttl,
        )
        self.last_trace = {}

    @staticmethod
    def _compact(strategy) -> dict:
        trace = getattr(strategy, 'last_trace', {}) or {}
        return dict(trace) if isinstance(trace, dict) else {}

    def _should_probe_liquidity(self, regime_metadata: dict | None) -> bool:
        if not _env_bool('STRATEGY_ROUTER_LIQUIDITY_PROBE_ENABLED', False):
            return False
        meta = regime_metadata or {}
        candidate = str(meta.get('candidate') or 'UNKNOWN')
        confidence = float(meta.get('confidence') or 0.0)
        scores = meta.get('scores') if isinstance(meta.get('scores'), dict) else {}
        features = meta.get('features') if isinstance(meta.get('features'), dict) else {}

        min_vol_score = max(_env_int('STRATEGY_ROUTER_LIQUIDITY_PROBE_MIN_VOL_SCORE', 3), 1)
        min_confidence = max(_env_float('STRATEGY_ROUTER_LIQUIDITY_PROBE_MIN_CONFIDENCE', 0.42), 0.0)
        volatile_score = float(scores.get('VOLATILE_SWEEP') or 0.0)
        wick = float(features.get('wick_instability') or 0.0)
        failure = float(features.get('breakout_failure_ratio') or 0.0)
        btc_shock = float(features.get('btc_shock_ratio') or 0.0)
        atr_pct = float(features.get('atr_pct') or 0.0)

        if candidate == 'VOLATILE_SWEEP' and confidence >= min_confidence:
            return True
        return (
            volatile_score >= min_vol_score
            and (
                wick >= 0.46
                or failure >= 0.18
                or (btc_shock >= 1.05 and atr_pct >= 0.0055)
            )
        )

    def discover_armed(self, regime, snapshot, symbol, timeframe, regime_metadata=None):
        setup = self.armed.discover(
            regime, snapshot, symbol, timeframe, regime_metadata=regime_metadata
        )
        self.last_trace = {
            "selected": None,
            "reason": "setup_armed" if setup is not None else "no_armable_setup",
            "armed": dict(self.armed.last_trace or {}),
        }
        return setup

    def trigger_armed(self, setup, snapshot, decision_id):
        status, intent, trace = self.armed.trigger(setup, snapshot, decision_id)
        self.last_trace = {
            "selected": getattr(getattr(intent, "strategy", None), "value", None) if intent else None,
            "reason": f"armed_{status}",
            "armed": {"status": status, **dict(trace or {})},
        }
        return status, intent, trace

    def evaluate(self, regime, candles, decision_id, symbol, timeframe,
                 current_price=None, snapshot=None, regime_metadata=None):
        if regime.hard_block:
            self.last_trace = {
                'selected': None,
                'reason': 'regime_hard_block',
                'breakout': {'accepted': False, 'reason': 'not_run'},
                'sweep': {'accepted': False, 'reason': 'not_run'},
            }
            return None

        # Source enforced router: RANGE has no executable strategy. The source's
        # range_mean_reversion remains a shadow strategy, not an order source.
        if not regime.breakout_allowed and not regime.sweep_allowed:
            self.last_trace = {
                'selected': None,
                'reason': 'router_regime_no_trade',
                'breakout': {'accepted': False, 'reason': 'regime_breakout_not_allowed'},
                'sweep': {'accepted': False, 'reason': 'regime_sweep_not_allowed'},
            }
            return None

        traces = {}
        selected = None

        if regime.breakout_allowed:
            selected = self.breakout.evaluate(
                regime, candles, decision_id, symbol, timeframe,
                current_price, snapshot=snapshot,
            )
            traces['breakout'] = self._compact(self.breakout)
            if selected is not None:
                traces['sweep'] = {'accepted': False, 'reason': 'primary_breakout_selected'}
            elif self._should_probe_liquidity(regime_metadata):
                probe = self.sweep.evaluate(
                    (replace(regime, sweep_allowed=True) if is_dataclass(regime) else SimpleNamespace(**{**vars(regime), "sweep_allowed": True})), candles, decision_id, symbol, timeframe,
                    current_price, snapshot=snapshot,
                )
                traces['sweep'] = self._compact(self.sweep)
                probe_min_score = max(
                    _env_float('STRATEGY_ROUTER_LIQUIDITY_PROBE_MIN_SIGNAL_SCORE', 74.0),
                    0.0,
                )
                if probe is not None and float(probe.quality) >= probe_min_score:
                    selected = probe
                    traces['router_probe'] = {
                        'accepted': True,
                        'reason': 'trend_liquidity_probe_selected',
                        'score': float(probe.quality),
                    }
                elif probe is not None:
                    traces['router_probe'] = {
                        'accepted': False,
                        'reason': 'trend_liquidity_probe_below_min_score',
                        'score': float(probe.quality),
                    }
            else:
                traces['sweep'] = {'accepted': False, 'reason': 'liquidity_probe_disabled_or_gate_not_met'}
        else:
            traces['breakout'] = {'accepted': False, 'reason': 'regime_breakout_not_allowed'}
            selected = self.sweep.evaluate(
                regime, candles, decision_id, symbol, timeframe,
                current_price, snapshot=snapshot,
            )
            traces['sweep'] = self._compact(self.sweep)

        self.last_trace = {
            'selected': getattr(getattr(selected, 'strategy', None), 'value', None) if selected else None,
            'reason': 'mapped_strategy_selected' if selected else 'no_valid_setup',
            **traces,
        }
        return selected
