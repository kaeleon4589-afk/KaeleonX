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
from app.auth.service import AuthService
from app.telegram.notifications import TelegramTradeNotifier
from app.orchestrator import TradingOrchestrator
from app.trading.profile import UserTradingProfileService
from app.trading.metrics import calculate_performance
from app.models.trading import Position
from app.models.enums import Direction


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
        self.notifier = TelegramTradeNotifier(settings, db, AuthService(db), audit=audit)
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
                self.settings.paper_initial_equity,
            )
            local_exits = True

        position_manager = PositionManager(
            ExitEngine(),
            self.audit,
            db=self.db,
            owner_user_id=user_id,
            owner_mode=mode,
            evaluate_local_exits=local_exits,
            on_closed=lambda position: self.notifier.position_closed(user_id, mode, position),
        )
        if hasattr(execution, "on_realized"):
            position_manager.on_realized = execution.on_realized

        # Recover persisted open positions after worker restarts so duplicate entries
        # are not created before the exchange/local state is reconciled.
        for row in self.db.find_many("positions", {"user_id": user_id, "status": "OPEN", "mode": mode}, limit=1000):
            try:
                direction = row.get("direction")
                if not isinstance(direction, Direction):
                    direction = Direction(str(direction))
                restored = Position(
                    position_id=str(row["position_id"]),
                    decision_id=str(row.get("decision_id", "RECOVERED")),
                    symbol=str(row.get("symbol", "")),
                    direction=direction,
                    quantity=float(row.get("quantity", 0)),
                    entry_price=float(row.get("entry_price", 0)),
                    stop_price=float(row.get("stop_price", 0)),
                    target_price=float(row.get("target_price", 0)),
                    status=str(row.get("status", "OPEN")),
                    realized_pnl=float(row.get("realized_pnl", 0)),
                    unrealized_pnl=float(row.get("unrealized_pnl", 0)),
                    tp1_price=row.get("tp1_price"),
                    tp2_price=row.get("tp2_price"),
                    remaining_quantity=row.get("remaining_quantity"),
                    tp1_hit=bool(row.get("tp1_hit", False)),
                    stop_moved_to_breakeven=bool(row.get("stop_moved_to_breakeven", False)),
                    entry_fee=float(row.get("entry_fee", 0)),
                    exit_fee=float(row.get("exit_fee", 0)),
                    funding_pnl=float(row.get("funding_pnl", 0)),
                    opened_at=row.get("opened_at"),
                    closed_at=row.get("closed_at"),
                    exit_price=row.get("exit_price"),
                    exit_reason=row.get("exit_reason"),
                )
                for attr in ("strategy", "quality", "execution_rr", "structural_rr"):
                    if row.get(attr) is not None:
                        setattr(restored, attr, row.get(attr))
                position_manager.positions[str(row["position_id"])] = restored
            except (KeyError, TypeError, ValueError) as exc:
                self.audit.event("POSITION_RESTORE_ERROR", user_id, user_id=user_id, mode=mode, position_id=row.get("position_id"), error=str(exc))

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
            execution_mode=mode,
            on_position_opened=lambda position: self.notifier.position_opened(user_id, mode, position),
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
                    self.audit.event("USER_RUNTIME_CONFIG_ERROR", uid, user_id=uid, error=str(exc))
                    self._runtimes.pop(uid, None)

            for uid in list(self._runtimes):
                if uid not in live_user_ids:
                    self._runtimes.pop(uid, None)
            self._last_refresh = now

    async def run_snapshot(self, snapshot) -> None:
        await self.refresh()
        for runtime in list(self._runtimes.values()):
            try:
                self.audit.event('USER_MARKET_ANALYSIS_START', runtime.user_id, level='DEBUG', persist=False, user_id=runtime.user_id, mode=runtime.mode, symbol=snapshot.symbol, trading_enabled=runtime.trading_enabled, configured_capital=runtime.configured_capital)
                live_allowed = True
                if runtime.mode == TradingEnvironment.LIVE.value:
                    live_allowed = self.billing.entitlement(runtime.user_id).live_allowed

                if hasattr(runtime.execution, "get_equity"):
                    available_equity = await runtime.execution.get_equity()
                    self.profiles.update_available_equity(runtime.user_id, available_equity)
                else:
                    # DEMO always owns a fixed virtual wallet. The configured
                    # capital is only the portion allocated to KAELEON.
                    available_equity = float(self.settings.paper_initial_equity)

                effective_capital = min(runtime.configured_capital, float(available_equity))
                if effective_capital < self.settings.min_operating_capital:
                    self.audit.event('ENTRY_BLOCKED', runtime.user_id, user_id=runtime.user_id, mode=runtime.mode, symbol=snapshot.symbol, reason='insufficient_capital', effective_capital=effective_capital, minimum=self.settings.min_operating_capital)
                    self._persist_state(runtime, available_equity, effective_capital, "INSUFFICIENT_CAPITAL", snapshot)
                    continue

                allow_entries = runtime.trading_enabled and live_allowed
                if not allow_entries:
                    self.audit.event('ENTRY_BLOCKED', runtime.user_id, level='DEBUG', persist=False, user_id=runtime.user_id, mode=runtime.mode, symbol=snapshot.symbol, reason='trading_paused' if not runtime.trading_enabled else 'live_not_entitled')
                result = await runtime.orchestrator.on_snapshot(
                    snapshot,
                    effective_capital,
                    user_id=runtime.user_id,
                    allow_entries=allow_entries,
                )
                if runtime.mode == TradingEnvironment.LIVE.value and not live_allowed:
                    status = "LIVE_NO_ENTITLED"
                else:
                    status = "ACTIVO" if runtime.trading_enabled else "PAUSADO"
                if result and result.get("filled"):
                    status = "OPERANDO"
                self._persist_state(runtime, available_equity, effective_capital, status, snapshot)
            except Exception as exc:
                self.audit.event("USER_RUNTIME_ERROR", runtime.user_id, user_id=runtime.user_id, mode=runtime.mode, symbol=snapshot.symbol, error=str(exc))
                self._persist_state(runtime, 0.0, 0.0, "ERROR", snapshot)

    def _persist_state(self, runtime: UserRuntime, available_equity: float, effective_capital: float, status: str, snapshot=None) -> None:
        open_position = next(
            (p.__dict__ for p in runtime.position_manager.positions.values() if p.status == "OPEN"),
            None,
        )
        positions = self.db.find_many("positions", {"user_id": runtime.user_id, "mode": runtime.mode}, limit=10000)
        metrics = calculate_performance(positions, runtime.configured_capital)
        state_key = {"user_id": runtime.user_id, "mode": runtime.mode.upper()}
        regime_meta = getattr(runtime.orchestrator.regime_engine, 'last_metadata', {}) or {}
        strategy_trace = getattr(runtime.orchestrator.router, 'last_trace', {}) or {}
        self.db.upsert("user_engine_state", state_key, {
            "user_id": runtime.user_id,
            "mode": runtime.mode.upper(),
            "status": status,
            "configured_capital": runtime.configured_capital,
            "capital": effective_capital,
            "available_equity": float(available_equity),
            "trading_enabled": runtime.trading_enabled,
            "coinw_connected": self.profiles.public(runtime.user_id).coinw_verified,
            "open_position": open_position,
            "markets_scanned": int(getattr(snapshot, 'markets_scanned', 0) or 0),
            "candidates": int(getattr(snapshot, 'candidates', 0) or 0),
            "last_symbol": getattr(snapshot, 'symbol', None),
            "last_price": getattr(snapshot, 'last', None),
            "regime": regime_meta.get('active'),
            "regime_candidate": regime_meta.get('candidate'),
            "regime_confidence": regime_meta.get('confidence'),
            "strategy": strategy_trace.get('selected'),
            "last_strategy_trace": strategy_trace,
            **metrics,
        })
