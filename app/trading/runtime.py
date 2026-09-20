from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass

from app.coinw.live_adapter import build_live_adapter_from_credentials
from app.execution.demo import DemoExecutionEngine
from app.execution.live import LiveExecutionEngine
from app.execution.environments import TradingEnvironment
from app.logging.logger import AuditLogger
from app.position.exit_engine import ExitEngine
from app.position.manager import PositionManager
from app.regime.regime_engine import RegimeEngine
from app.risk.manager import RiskManager
from app.strategy.router import StrategyRouter
from app.market.signal_factory import SignalFactory
from app.billing.service import BillingService
from app.orchestrator import TradingOrchestrator
from app.trading.profile import UserTradingProfileService


@dataclass
class UserRuntime:
    user_id: str
    fingerprint: str
    mode: str
    trading_enabled: bool
    configured_capital: float
    execution: object
    position_manager: PositionManager
    orchestrator: TradingOrchestrator


class UserTradingRuntimeManager:
    """Runs the same market snapshot through isolated per-user trading runtimes.

    Market data is shared; exchange credentials, positions, equity and execution are
    isolated per user. This removes global CoinW credentials from the trading worker.
    """

    def __init__(self, settings, db, audit: AuditLogger, profile_service: UserTradingProfileService):
        self.settings = settings
        self.db = db
        self.audit = audit
        self.profiles = profile_service
        self.billing = BillingService(db, trial_days=settings.live_trial_days)
        self.signal_factory = SignalFactory()
        self._runtimes: dict[str, UserRuntime] = {}
        self._last_refresh = 0.0
        self._lock = asyncio.Lock()

    @staticmethod
    def _fingerprint(row: dict) -> str:
        # Runtime rebuilds are required only when execution mode or credentials
        # change. Capital/enable toggles update the existing runtime so DEMO
        # positions are not accidentally discarded.
        raw = "|".join([
            str(row.get("execution_mode", "demo")),
            str(row.get("coinw_api_key_encrypted") or ""),
            str(row.get("coinw_api_secret_encrypted") or ""),
        ])
        return hashlib.sha256(raw.encode()).hexdigest()

    async def _build_runtime(self, user_id: str, row: dict, fingerprint: str) -> UserRuntime:
        mode = str(row.get("execution_mode", "demo"))
        enabled = bool(row.get("trading_enabled", False))
        capital = float(row.get("operating_capital", self.settings.min_operating_capital))
        if mode == TradingEnvironment.LIVE.value:
            creds = self.profiles.credentials(user_id)
            adapter = build_live_adapter_from_credentials(
                creds.api_key,
                creds.api_secret,
                base_url=self.settings.coinw_rest_base_url,
                audit=self.audit,
            )
            execution = LiveExecutionEngine(adapter, leverage=self.settings.fixed_leverage)
            local_exits = False
        else:
            execution = DemoExecutionEngine(
                self.audit,
                self.settings.paper_taker_fee,
                self.settings.paper_slippage_bps,
                self.settings.paper_max_spread_bps,
                capital,
            )
            local_exits = True

        position_manager = PositionManager(
            ExitEngine(),
            self.audit,
            db=self.db,
            owner_user_id=user_id,
            evaluate_local_exits=local_exits,
        )
        if hasattr(execution, "on_realized"):
            position_manager.on_realized = execution.on_realized

        orchestrator = TradingOrchestrator(
            RegimeEngine(),
            StrategyRouter(),
            RiskManager(
                self.settings.risk_per_trade,
                self.settings.fixed_leverage,
                max_margin_fraction=self.settings.max_margin_fraction,
            ),
            execution,
            self.db,
            self.audit,
            position_manager,
            self.signal_factory,
        )
        return UserRuntime(
            user_id=user_id,
            fingerprint=fingerprint,
            mode=mode,
            trading_enabled=enabled,
            configured_capital=capital,
            execution=execution,
            position_manager=position_manager,
            orchestrator=orchestrator,
        )

    async def refresh(self) -> None:
        now = time.monotonic()
        if now - self._last_refresh < self.settings.user_runtime_refresh_seconds:
            return
        async with self._lock:
            now = time.monotonic()
            if now - self._last_refresh < self.settings.user_runtime_refresh_seconds:
                return
            users = self.db.find_many("users", {"status": "active"}, limit=self.settings.max_active_users)
            live_user_ids = set()
            for user in users:
                uid = str(user["user_id"])
                profile = self.profiles.get(uid)
                if not profile:
                    continue
                live_user_ids.add(uid)
                fp = self._fingerprint(profile)
                current = self._runtimes.get(uid)
                if current and current.fingerprint == fp:
                    # Keep the execution/position state intact while applying
                    # user setting changes such as capital or enable/disable.
                    current.trading_enabled = bool(profile.get("trading_enabled", False))
                    current.configured_capital = float(profile.get("operating_capital", self.settings.min_operating_capital))
                    continue
                try:
                    self._runtimes[uid] = await self._build_runtime(uid, profile, fp)
                except Exception as exc:
                    self.audit.event("USER_RUNTIME_CONFIG_ERROR", uid, error=str(exc))
                    self._runtimes.pop(uid, None)

            for uid in list(self._runtimes):
                if uid not in live_user_ids:
                    self._runtimes.pop(uid, None)
            self._last_refresh = now

    async def run_snapshot(self, snapshot) -> None:
        await self.refresh()
        for runtime in list(self._runtimes.values()):
            try:
                live_allowed = True
                if runtime.mode == TradingEnvironment.LIVE.value:
                    live_allowed = self.billing.entitlement(runtime.user_id).live_allowed

                if hasattr(runtime.execution, "get_equity"):
                    available_equity = await runtime.execution.get_equity()
                else:
                    available_equity = float(getattr(runtime.execution, "equity", runtime.configured_capital))

                effective_capital = min(runtime.configured_capital, float(available_equity))
                if effective_capital < self.settings.min_operating_capital:
                    self._persist_state(runtime, available_equity, effective_capital, "INSUFFICIENT_CAPITAL")
                    continue

                result = await runtime.orchestrator.on_snapshot(
                    snapshot,
                    effective_capital,
                    user_id=runtime.user_id,
                    allow_entries=runtime.trading_enabled and live_allowed,
                )
                if runtime.mode == TradingEnvironment.LIVE.value and not live_allowed:
                    status = "LIVE_NO_ENTITLED"
                else:
                    status = "ACTIVO" if runtime.trading_enabled else "PAUSADO"
                if result and result.get("filled"):
                    status = "OPERANDO"
                self._persist_state(runtime, available_equity, effective_capital, status)
            except Exception as exc:
                self.audit.event("USER_RUNTIME_ERROR", runtime.user_id, symbol=snapshot.symbol, error=str(exc))
                self._persist_state(runtime, 0.0, 0.0, "ERROR")

    def _persist_state(self, runtime: UserRuntime, available_equity: float, effective_capital: float, status: str) -> None:
        open_position = next(
            (p.__dict__ for p in runtime.position_manager.positions.values() if p.status == "OPEN"),
            None,
        )
        self.db.upsert("user_engine_state", {"user_id": runtime.user_id}, {
            "user_id": runtime.user_id,
            "mode": runtime.mode.upper(),
            "status": status,
            "configured_capital": runtime.configured_capital,
            "capital": effective_capital,
            "available_equity": float(available_equity),
            "trading_enabled": runtime.trading_enabled,
            "coinw_connected": runtime.mode == "live" and available_equity > 0,
            "open_position": open_position,
        })
