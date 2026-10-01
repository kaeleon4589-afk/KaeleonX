from __future__ import annotations

from dataclasses import replace
import os

from app.models.enums import Strategy
from app.models.trading import TradeIntent
from app.regime.v2 import evaluate_snapshot_once, TREND, RANGE, SHOCK, DEAD, TRANSITION
from app.strategy.breakout_retest_v2 import BreakoutRetestStrategyV2
from app.strategy.entry_engine_v2 import EntryLifecycleV2
from app.strategy.liquidity_sweep_v2 import LiquiditySweepStrategyV2
from app.strategy.armed_entry import ArmedEntryEngine as LegacyArmedEntryEngine
from app.strategy.prearm_policy import prearm_policy


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() not in {"0", "false", "no", "off", ""}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


class StrategyRouter:
    """KAELEON V2 router.

    TREND -> BREAKOUT_RETEST_V2
    RANGE -> LIQUIDITY_SWEEP_V2
    SHOCK / DEAD / TRANSITION -> NO_TRADE

    The legacy strategy modules remain in the repository as rollback/reference,
    but they are no longer imported by the production router.
    """

    def __init__(
        self,
        armed_ttl_seconds: float | None = None,
        armed_chase_tolerance_atr: float | None = None,
        armed_trigger_close_tolerance_atr: float | None = None,
        armed_consumed_ttl_seconds: float | None = None,
        armed_fast_confirm_enabled: bool | None = None,
        armed_fast_confirm_max_age_seconds: float | None = None,
        setup_watch_ttl_seconds: float | None = None,
    ):
        self.breakout = BreakoutRetestStrategyV2()
        self.sweep = LiquiditySweepStrategyV2()
        self.armed = EntryLifecycleV2(
            ttl_seconds=_env_float("TRADE_ARMED_SETUP_TTL_SECONDS", 600.0) if armed_ttl_seconds is None else armed_ttl_seconds,
            watch_ttl_seconds=_env_float("TRADE_SETUP_WATCH_TTL_SECONDS", 1800.0) if setup_watch_ttl_seconds is None else setup_watch_ttl_seconds,
            consumed_ttl_seconds=_env_float("TRADE_ARMED_CONSUMED_TTL_SECONDS", 3600.0) if armed_consumed_ttl_seconds is None else armed_consumed_ttl_seconds,
            chase_tolerance_atr=_env_float("TRADE_ARMED_CHASE_TOLERANCE_ATR", 0.15) if armed_chase_tolerance_atr is None else armed_chase_tolerance_atr,
            trigger_close_tolerance_atr=_env_float("TRADE_ARMED_TRIGGER_CLOSE_TOLERANCE_ATR", 0.08) if armed_trigger_close_tolerance_atr is None else armed_trigger_close_tolerance_atr,
            fast_confirm_enabled=_env_bool("TRADE_ARMED_FAST_CONFIRM_ENABLED", True) if armed_fast_confirm_enabled is None else armed_fast_confirm_enabled,
            fast_confirm_max_age_seconds=_env_float("TRADE_ARMED_FAST_CONFIRM_MAX_AGE_SECONDS", 30.0) if armed_fast_confirm_max_age_seconds is None else armed_fast_confirm_max_age_seconds,
        )
        self.legacy = LegacyArmedEntryEngine(
            ttl_seconds=_env_float("TRADE_ARMED_SETUP_TTL_SECONDS", 600.0) if armed_ttl_seconds is None else armed_ttl_seconds,
            chase_tolerance_atr=_env_float("TRADE_ARMED_CHASE_TOLERANCE_ATR", 0.15) if armed_chase_tolerance_atr is None else armed_chase_tolerance_atr,
            trigger_close_tolerance_atr=_env_float("TRADE_ARMED_TRIGGER_CLOSE_TOLERANCE_ATR", 0.08) if armed_trigger_close_tolerance_atr is None else armed_trigger_close_tolerance_atr,
            consumed_ttl_seconds=_env_float("TRADE_ARMED_CONSUMED_TTL_SECONDS", 3600.0) if armed_consumed_ttl_seconds is None else armed_consumed_ttl_seconds,
            fast_confirm_enabled=_env_bool("TRADE_ARMED_FAST_CONFIRM_ENABLED", True) if armed_fast_confirm_enabled is None else armed_fast_confirm_enabled,
            fast_confirm_max_age_seconds=_env_float("TRADE_ARMED_FAST_CONFIRM_MAX_AGE_SECONDS", 30.0) if armed_fast_confirm_max_age_seconds is None else armed_fast_confirm_max_age_seconds,
            watch_ttl_seconds=_env_float("TRADE_SETUP_WATCH_TTL_SECONDS", 1800.0) if setup_watch_ttl_seconds is None else setup_watch_ttl_seconds,
        )
        self.last_trace: dict = {}

    @staticmethod
    def _active(regime_metadata: dict | None) -> str:
        return str((regime_metadata or {}).get("active") or "")

    @staticmethod
    def _legacy_context(regime_metadata: dict | None) -> bool:
        # Only explicit pre-V2 labels use the compatibility engine. Production
        # RegimeEngineV2 emits TREND/RANGE/SHOCK/DEAD/TRANSITION.
        return str((regime_metadata or {}).get("active") or "") in {
            "TREND_CONTINUATION", "VOLATILE_SWEEP", "UNKNOWN"
        }

    def _scan(self, regime, snapshot, *, allow_watch: bool = True):
        if getattr(regime, "hard_block", False):
            return None, {"reason": "regime_hard_block"}
        if getattr(regime, "breakout_allowed", False):
            candidate = self.breakout.scan(regime, snapshot, allow_watch=allow_watch)
            return candidate, {"breakout": dict(self.breakout.last_trace or {})}
        if getattr(regime, "sweep_allowed", False):
            candidate = self.sweep.scan(regime, snapshot, allow_watch=allow_watch)
            return candidate, {"sweep": dict(self.sweep.last_trace or {})}
        return None, {"reason": "router_regime_no_trade"}

    def discover_armed(self, regime, snapshot, symbol, timeframe, regime_metadata=None):
        if self._legacy_context(regime_metadata):
            setup = self.legacy.discover(regime, snapshot, symbol, timeframe, regime_metadata=regime_metadata)
            if setup is not None:
                setup = replace(setup, metadata={**dict(setup.metadata or {}), "legacy_compat_context": True})
            trace = dict(self.legacy.last_trace or {})
            self.last_trace = {
                "selected": getattr(getattr(setup, "strategy", None), "value", None) if setup else None,
                "reason": "legacy_setup_armed" if setup else str(trace.get("reason") or "no_armable_setup"),
                "engine": "legacy_compat",
                "prearm_profile": prearm_policy().name,
                "armed": trace,
            }
            return setup
        candidate, branches = self._scan(regime, snapshot, allow_watch=True)
        if candidate is None or candidate.stage != "READY":
            reason = "no_complete_setup" if candidate is None else f"candidate_{candidate.stage.lower()}"
            self.last_trace = {
                "selected": None,
                "reason": reason,
                "engine": "v2",
                "regime": self._active(regime_metadata),
                "armed": {"accepted": False, "reason": reason, **branches},
            }
            return None
        setup = self.armed.arm(candidate, symbol, timeframe)
        if setup is None:
            self.last_trace = {
                "selected": None,
                "reason": "setup_consumed",
                "engine": "v2",
                "armed": {"accepted": False, "reason": "setup_consumed", **branches},
            }
            return None
        self.last_trace = {
            "selected": candidate.strategy.value,
            "reason": "setup_armed",
            "engine": "v2",
            "regime": self._active(regime_metadata),
            "armed": {
                "accepted": True,
                "reason": "setup_armed",
                "strategy": candidate.strategy.value,
                "direction": candidate.direction.value,
                "quality": candidate.quality,
                **branches,
            },
        }
        return setup

    def discover_watch(self, regime, snapshot, symbol, timeframe, regime_metadata=None):
        if self._legacy_context(regime_metadata):
            watch = self.legacy.discover_watch(regime, snapshot, symbol, timeframe, regime_metadata=regime_metadata)
            if watch is not None:
                watch = replace(watch, metadata={**dict(watch.metadata or {}), "legacy_compat_context": True})
            branches = dict(self.legacy.watch_trace or {})
            primary = "setup_watching" if watch is not None else str(branches.get("reason") or "no_watchable_precursor")
            self.last_trace = {
                "selected": None,
                "reason": primary,
                "engine": "legacy_compat",
                "prearm_profile": prearm_policy().name,
                "rejection_reasons": {k: v.get("reason") for k, v in branches.items() if isinstance(v, dict)},
                "watch": {
                    "accepted": watch is not None, "reason": primary, "branches": branches,
                    "strategy": getattr(getattr(watch, "strategy", None), "value", None) if watch else None,
                    "direction": getattr(getattr(watch, "direction", None), "value", None) if watch else None,
                },
            }
            return watch
        candidate, branches = self._scan(regime, snapshot, allow_watch=True)
        if candidate is None or candidate.stage != "WATCH":
            reason = "no_watchable_precursor" if candidate is None else "complete_setup_already_handled"
            self.last_trace = {
                "selected": None,
                "reason": reason,
                "engine": "v2",
                "regime": self._active(regime_metadata),
                "prearm_profile": "v2",
                "rejection_reasons": {k: v.get("reason") for k, v in branches.items() if isinstance(v, dict)},
                "watch": {"accepted": False, "reason": reason, "branches": branches},
            }
            return None
        watch = self.armed.watch(candidate, symbol, timeframe)
        if watch is None:
            reason = "watch_consumed"
            self.last_trace = {
                "selected": None,
                "reason": reason,
                "engine": "v2",
                "watch": {"accepted": False, "reason": reason, "branches": branches},
            }
            return None
        self.last_trace = {
            "selected": None,
            "reason": "setup_watching",
            "engine": "v2",
            "regime": self._active(regime_metadata),
            "watch": {
                "accepted": True,
                "reason": "setup_watching",
                "strategy": candidate.strategy.value,
                "direction": candidate.direction.value,
                "quality": candidate.quality,
                "branches": branches,
            },
        }
        return watch

    def consume_watch(self, watch, reason: str) -> None:
        if (watch.metadata or {}).get("engine_version") == "v2":
            self.armed.consume_watch(watch, reason)
        else:
            self.legacy.consume_watch(watch, reason)

    def restore_consumed_watch(self, watch_id: str, until_ms: int, reason: str = "restored") -> bool:
        a = self.armed.restore_consumed_watch(watch_id, until_ms, reason)
        b = self.legacy.restore_consumed_watch(watch_id, until_ms, reason)
        return bool(a or b)

    def consumed_watch_record(self, watch_id: str):
        return self.armed.consumed_watch_record(watch_id)

    def advance_watch(self, watch, snapshot):
        if (watch.metadata or {}).get("engine_version") != "v2":
            if not (watch.metadata or {}).get("legacy_compat_context"):
                trace = {"reason": "legacy_watch_not_migrated"}
                self.last_trace = {"selected": None, "reason": "watch_cancelled", "engine": "v2", "watch": {"status": "cancelled", **trace}}
                return "cancelled", None, trace
            status, setup, trace = self.legacy.advance_watch(watch, snapshot)
            if setup is not None:
                setup = replace(setup, metadata={**dict(setup.metadata or {}), "legacy_compat_context": True})
            self.last_trace = {
                "selected": getattr(getattr(setup, "strategy", None), "value", None) if setup else None,
                "reason": f"watch_{status}", "engine": "legacy_compat",
                "watch": {"status": status, **dict(trace or {})},
            }
            return status, setup, trace
        now_ms = int(__import__("time").time() * 1000)
        if now_ms >= int(watch.expires_at_ms):
            trace = {"reason": "watch_expired"}
            self.last_trace = {"selected": None, "reason": "watch_cancelled", "watch": {"status": "cancelled", **trace}, "engine": "v2"}
            return "cancelled", None, trace
        invalidated, invalid_reason = self.armed.watch_invalidated(watch, snapshot)
        if invalidated:
            trace = {"reason": invalid_reason}
            self.last_trace = {"selected": None, "reason": "watch_cancelled", "watch": {"status": "cancelled", **trace}, "engine": "v2"}
            return "cancelled", None, trace

        regime, regime_meta = evaluate_snapshot_once(snapshot)
        # WATCHING tolerates a short transition, but never a shock/dead market.
        active = str(regime_meta.get("active") or regime_meta.get("candidate") or "")
        if active in {SHOCK, DEAD}:
            trace = {"reason": f"watch_regime_{active.lower()}"}
            self.last_trace = {"selected": None, "reason": "watch_cancelled", "watch": {"status": "cancelled", **trace}, "engine": "v2"}
            return "cancelled", None, trace

        if watch.strategy == Strategy.BREAKOUT_RETEST:
            if active != TREND and not getattr(regime, "breakout_allowed", False):
                trace = {"reason": "watch_waiting_trend_regime", "regime": active}
                self.last_trace = {"selected": None, "reason": "watch_pending", "watch": {"status": "pending", **trace}, "engine": "v2"}
                return "pending", None, trace
            candidate = self.breakout.scan(regime, snapshot, allow_watch=True)
            branch = dict(self.breakout.last_trace or {})
        else:
            if active not in {RANGE, TRANSITION} and not getattr(regime, "sweep_allowed", False):
                trace = {"reason": "watch_waiting_range_regime", "regime": active}
                self.last_trace = {"selected": None, "reason": "watch_pending", "watch": {"status": "pending", **trace}, "engine": "v2"}
                return "pending", None, trace
            # If a one-bar transition appears, evaluate with a range-compatible
            # shadow permission rather than throwing away an otherwise valid sweep.
            if not getattr(regime, "sweep_allowed", False):
                from dataclasses import replace as dc_replace
                regime = dc_replace(regime, sweep_allowed=True, hard_block=False)
            candidate = self.sweep.scan(regime, snapshot, allow_watch=True)
            branch = dict(self.sweep.last_trace or {})

        if candidate is None:
            reason = str(branch.get("reason") or "watch_precursor_not_complete")
            terminal_reasons = {
                "breakout_failed_below_level", "breakout_failed_above_level", "entry_chased_after_retest",
                "sweep_reclaim_lost", "stop_geometry_invalid", "target_geometry_invalid",
            }
            if reason in terminal_reasons:
                trace = {"reason": reason, "branch": branch}
                self.last_trace = {"selected": None, "reason": "watch_cancelled", "watch": {"status": "cancelled", **trace}, "engine": "v2"}
                return "cancelled", None, trace
            trace = {"reason": reason, "branch": branch, "regime": active}
            self.last_trace = {"selected": None, "reason": "watch_pending", "watch": {"status": "pending", **trace}, "engine": "v2"}
            return "pending", None, trace

        # Do not let a tracked edge silently change side.
        if candidate.direction != watch.direction or candidate.strategy != watch.strategy:
            trace = {"reason": "watch_structure_changed", "candidate_direction": candidate.direction.value}
            self.last_trace = {"selected": None, "reason": "watch_cancelled", "watch": {"status": "cancelled", **trace}, "engine": "v2"}
            return "cancelled", None, trace
        if candidate.stage != "READY":
            trace = {"reason": str(branch.get("reason") or "watch_pending"), "quality": candidate.quality, "regime": active}
            self.last_trace = {"selected": None, "reason": "watch_pending", "watch": {"status": "pending", **trace}, "engine": "v2"}
            return "pending", None, trace

        armed = self.armed.arm(candidate, watch.symbol, watch.timeframe, watch=watch)
        if armed is None:
            trace = {"reason": "setup_consumed"}
            self.last_trace = {"selected": None, "reason": "watch_cancelled", "watch": {"status": "cancelled", **trace}, "engine": "v2"}
            return "cancelled", None, trace
        trace = {"reason": "watch_promoted_to_armed", "quality": candidate.quality, "regime": active, "branch": branch}
        self.last_trace = {"selected": candidate.strategy.value, "reason": "watch_armed", "watch": {"status": "armed", **trace}, "engine": "v2"}
        return "armed", armed, trace

    def trigger_armed(self, setup, snapshot, decision_id):
        meta = dict(setup.metadata or {})
        if meta.get("engine_version") == "v2":
            engine = self.armed
        elif meta.get("legacy_compat_context"):
            engine = self.legacy
        else:
            status, intent, trace = "cancelled", None, {"reason": "legacy_setup_not_migrated"}
            self.last_trace = {
                "selected": None, "reason": "armed_cancelled", "engine": "v2",
                "armed": {"status": status, **trace},
            }
            return status, intent, trace
        status, intent, trace = engine.trigger(setup, snapshot, decision_id)
        self.last_trace = {
            "selected": getattr(getattr(intent, "strategy", None), "value", None) if intent else None,
            "reason": f"armed_{status}",
            "engine": "v2",
            "armed": {"status": status, **dict(trace or {})},
        }
        return status, intent, trace

    def evaluate(self, regime, candles, decision_id, symbol, timeframe, current_price=None, snapshot=None, regime_metadata=None):
        # Test/custom-router compatibility for the previous evaluate() contract.
        if hasattr(self.breakout, "evaluate") and not hasattr(self.breakout, "scan"):
            if getattr(regime, "hard_block", False) or (not getattr(regime, "breakout_allowed", False) and not getattr(regime, "sweep_allowed", False)):
                self.last_trace = {"selected": None, "reason": "router_regime_no_trade" if not getattr(regime, "hard_block", False) else "regime_hard_block"}
                return None
            if getattr(regime, "breakout_allowed", False):
                selected = self.breakout.evaluate(regime, candles, decision_id, symbol, timeframe, current_price, snapshot=snapshot)
                if selected is None and _env_bool("STRATEGY_ROUTER_LIQUIDITY_PROBE_ENABLED", False):
                    meta = regime_metadata or {}
                    if str(meta.get("candidate") or "") == "VOLATILE_SWEEP":
                        selected = self.sweep.evaluate(regime, candles, decision_id, symbol, timeframe, current_price, snapshot=snapshot)
                self.last_trace = {"selected": getattr(getattr(selected, "strategy", None), "value", None), "reason": "mapped_strategy_selected" if selected else "no_valid_setup"}
                return selected
            return self.sweep.evaluate(regime, candles, decision_id, symbol, timeframe, current_price, snapshot=snapshot)
        # Compatibility/rollback path. Production keeps armed entry enabled.
        candidate, branches = self._scan(regime, snapshot, allow_watch=False)
        if candidate is None or candidate.stage != "READY":
            self.last_trace = {"selected": None, "reason": "no_valid_setup", "engine": "v2", **branches}
            return None
        setup = self.armed.arm(candidate, symbol, timeframe)
        if setup is None:
            self.last_trace = {"selected": None, "reason": "setup_consumed", "engine": "v2"}
            return None
        # Without a micro snapshot there is deliberately no immediate order. This
        # keeps the compatibility path safe rather than bypassing V2 confirmation.
        self.last_trace = {
            "selected": candidate.strategy.value,
            "reason": "setup_requires_armed_confirmation",
            "engine": "v2",
            "armed": {"accepted": True, "reason": "setup_requires_armed_confirmation"},
        }
        return None
