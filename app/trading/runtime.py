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
from app.trading.persistence import TradePersistence
from app.trading.metrics import calculate_performance, position_net_pnl
from app.trading.statistics import TradingStatistics
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
    coinw_verified: bool = False
    live_allowed: bool = True
    last_state_persist: float = 0.0
    last_state_signature: str = ""
    last_reset_id: str | None = None


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
        self.entry_guard = None
        self._last_refresh = 0.0
        self._lock = asyncio.Lock()
        self._processing_lock = asyncio.Lock()

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
        public = await asyncio.to_thread(self.profiles.public, user_id)
        capital = float(public.operating_capital)
        if mode == TradingEnvironment.LIVE.value:
            creds = await asyncio.to_thread(self.profiles.credentials, user_id)
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
                leverage=self.settings.fixed_leverage,
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
            on_opened=lambda position: self.notifier.position_opened(user_id, mode, position),
            persist_interval_seconds=self.settings.engine_state_persist_seconds,
        )
        if hasattr(execution, "on_realized"):
            position_manager.on_realized = execution.on_realized
            position_manager.exit_slippage_bps = self.settings.paper_slippage_bps

        # Recover product state before the market loop starts. DEMO equity is also
        # reconstructed from persisted PnL/fees so a worker restart does not reset
        # the virtual wallet back to 100 USDT.
        persisted_positions = await asyncio.to_thread(
            self.db.find_many, "positions", {"user_id": user_id, "mode": mode}, limit=0
        )
        if mode == TradingEnvironment.DEMO.value and hasattr(execution, "equity"):
            execution.equity = await asyncio.to_thread(self.profiles.demo_account.balance, user_id)
        for row in (x for x in persisted_positions if str(x.get("status", "OPEN")).upper() == "OPEN"):
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
                    revision=int(row.get('revision', 0)),
                    current_price=row.get('current_price'),
                    settlement_pending=bool(row.get('settlement_pending', False)),
                    net_pnl=row.get('net_pnl'),
                    leverage=int(row.get('leverage', self.settings.fixed_leverage)),
                    protected=bool(row.get('protected', True)),
                    exit_trigger_price=row.get('exit_trigger_price'),
                    stop_gap_bps=row.get('stop_gap_bps'),
                    exit_quote_delay_ms=row.get('exit_quote_delay_ms'),
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
                max_leverage=self.settings.fixed_leverage,
                fee_rate=self.settings.paper_taker_fee,
                exit_slippage_bps=self.settings.paper_slippage_bps,
            ),
            execution,
            self.db,
            self.audit,
            position_manager,
            self.signal_factory,
            execution_mode=mode,
            on_position_opened=lambda position: self.notifier.position_opened(user_id, mode, position),
            on_position_closed=lambda position: self.notifier.position_closed(user_id, mode, position),
            persistence=TradePersistence(
                self.db, self.audit,
                timeout_seconds=self.settings.trade_persist_timeout_seconds,
                retries=self.settings.trade_persist_retries,
            ),
        )
        orchestrator.entry_guard = self.entry_guard
        position_manager.persistence = orchestrator.persistence
        pending = await asyncio.to_thread(self.db.find_one, "execution_pending", {"user_id": user_id, "active": True})
        if pending and mode == "live":
            if any(p.decision_id == pending.get('decision_id') for p in position_manager.positions.values()):
                await asyncio.to_thread(self.db.upsert, 'execution_pending', {'user_id': user_id}, {'active': False})
            else:
                orchestrator.pending_execution = {**pending, "created_monotonic": time.monotonic()}
        return UserRuntime(
            user_id=user_id,
            fingerprint=fingerprint,
            mode=mode,
            trading_enabled=enabled,
            configured_capital=capital,
            execution=execution,
            position_manager=position_manager,
            orchestrator=orchestrator,
            coinw_verified=bool(row.get("coinw_verified", False)),
            live_allowed=True,
            last_reset_id=(TradingStatistics(self.db).current_period(mode) or {}).get('reset_id'),
        )

    async def refresh(self) -> None:
        now = time.monotonic()
        if now - self._last_refresh < self.settings.user_runtime_refresh_seconds:
            return
        async with self._lock:
            now = time.monotonic()
            if now - self._last_refresh < self.settings.user_runtime_refresh_seconds:
                return
            users = await asyncio.to_thread(
                self.db.find_many, "users", {}, limit=0
            )
            live_user_ids = set()
            for user in users:
                uid = str(user["user_id"])
                profile = await asyncio.to_thread(self.profiles.get, uid)
                if not profile:
                    continue
                if user.get('status') != 'active':
                    held = await asyncio.to_thread(self.db.find_many, 'positions', {'user_id': uid, 'status': 'OPEN'}, limit=1)
                    pending = await asyncio.to_thread(self.db.find_one, 'execution_pending', {'user_id': uid, 'active': True})
                    if not held and not pending:
                        continue
                    profile = {**profile, 'trading_enabled': False}
                live_user_ids.add(uid)
                fp = self._fingerprint(profile)
                mode = str(profile.get("execution_mode", "demo"))
                live_allowed = True
                if mode == TradingEnvironment.LIVE.value:
                    entitlement = await asyncio.to_thread(self.billing.entitlement, uid)
                    live_allowed = bool(entitlement.live_allowed)
                current = self._runtimes.get(uid)
                if current and current.fingerprint == fp:
                    # Keep the execution/position state intact while applying
                    # user setting changes such as capital or enable/disable.
                    current.trading_enabled = bool(profile.get("trading_enabled", False))
                    current.configured_capital = float((await asyncio.to_thread(self.profiles.public, uid)).operating_capital)
                    current.coinw_verified = bool(profile.get("coinw_verified", False))
                    current.live_allowed = live_allowed
                    continue
                try:
                    built = await self._build_runtime(uid, profile, fp)
                    built.live_allowed = live_allowed
                    self._runtimes[uid] = built
                except Exception as exc:
                    self.audit.event("USER_RUNTIME_CONFIG_ERROR", uid, user_id=uid, error=str(exc))
                    self._runtimes.pop(uid, None)

            for uid in list(self._runtimes):
                if uid not in live_user_ids:
                    self._runtimes.pop(uid, None)
            self._last_refresh = now

    def tracked_symbols(self):
        symbols = {p.symbol for runtime in self._runtimes.values()
                   for p in runtime.position_manager.positions.values() if p.status == 'OPEN'}
        symbols.update(runtime.orchestrator.pending_execution['symbol']
                       for runtime in self._runtimes.values() if runtime.orchestrator.pending_execution)
        return symbols

    async def run_snapshot(self, snapshot) -> None:
        async with self._processing_lock:
            await self._run_snapshot(snapshot)

    async def _run_snapshot(self, snapshot) -> None:
        await self.refresh()
        periods = None
        if not getattr(snapshot, 'monitor_only', False):
            periods = await asyncio.to_thread(lambda: {
                mode: TradingStatistics(self.db).current_period(mode) for mode in ('demo', 'live')
            })
        runtimes = list(self._runtimes.values())
        if getattr(snapshot, 'monitor_only', False):
            # A risk quote only needs the owners of an open position or an
            # unresolved order in this symbol. Visiting every other user would
            # delay stops as the account count grows.
            runtimes = [runtime for runtime in runtimes
                        if any(p.status == 'OPEN' and p.symbol == snapshot.symbol
                               for p in runtime.position_manager.positions.values())
                        or (runtime.orchestrator.pending_execution or {}).get('symbol') == snapshot.symbol]
        for runtime in runtimes:
            try:
                period = periods[runtime.mode] if periods is not None else None
                reset_id = period.get('reset_id') if period else None
                if periods is not None and runtime.last_reset_id != reset_id:
                    if runtime.mode == 'demo':
                        runtime.execution.equity = await asyncio.to_thread(
                            self.profiles.demo_account.balance, runtime.user_id)
                    runtime.last_reset_id = reset_id
                    runtime.last_state_persist = 0.0
                self.audit.event('USER_MARKET_ANALYSIS_START', runtime.user_id, level='DEBUG', persist=False, user_id=runtime.user_id, mode=runtime.mode, symbol=snapshot.symbol, trading_enabled=runtime.trading_enabled, configured_capital=runtime.configured_capital)
                live_allowed = runtime.live_allowed

                if hasattr(runtime.execution, "get_equity"):
                    try:
                        available_equity = await runtime.execution.get_equity()
                    except Exception as exc:
                        # Account balance failure must not prevent position reconciliation.
                        available_equity = 0.0
                        self.audit.event('ACCOUNT_SYNC_ERROR', runtime.user_id, level='ERROR',
                                         user_id=runtime.user_id, error=type(exc).__name__)
                else:
                    # DEMO always owns a fixed virtual wallet. The configured
                    # capital is only the portion allocated to KAELEON.
                    available_equity = float(self.settings.paper_initial_equity)

                effective_capital = min(runtime.configured_capital, float(available_equity))
                if effective_capital < self.settings.min_operating_capital:
                    self.audit.event('ENTRY_BLOCKED', runtime.user_id, user_id=runtime.user_id, mode=runtime.mode, symbol=snapshot.symbol, reason='insufficient_capital', effective_capital=effective_capital, minimum=self.settings.min_operating_capital)

                allow_entries = (runtime.trading_enabled and live_allowed
                                 and runtime.coinw_verified
                                 and effective_capital >= self.settings.min_operating_capital
                                 and not getattr(snapshot, 'monitor_only', False))
                if not allow_entries:
                    self.audit.event('ENTRY_BLOCKED', runtime.user_id, level='DEBUG', persist=False, user_id=runtime.user_id, mode=runtime.mode, symbol=snapshot.symbol, reason='trading_paused' if not runtime.trading_enabled else 'live_not_entitled')
                result = await runtime.orchestrator.on_snapshot(
                    snapshot,
                    effective_capital,
                    user_id=runtime.user_id,
                    allow_entries=allow_entries,
                    available_equity=available_equity,
                )
                if runtime.mode == TradingEnvironment.LIVE.value and not live_allowed:
                    status = "LIVE_NO_ENTITLED"
                else:
                    status = "ACTIVO" if runtime.trading_enabled else "PAUSADO"
                if result and result.get("filled"):
                    status = "OPERANDO"
                state_changed = runtime.position_manager.consume_state_changed()
                # Capture fees and realized PnL after this snapshot's open/close.
                if runtime.mode == 'demo':
                    available_equity = await runtime.execution.get_equity()
                    profile = await asyncio.to_thread(self.profiles.get, runtime.user_id)
                    if profile.get('demo_auto_compound', False):
                        runtime.configured_capital = max(0.0, available_equity)
                    effective_capital = min(runtime.configured_capital, max(0.0, available_equity))
                if any(p.status == 'OPEN' for p in runtime.position_manager.positions.values()):
                    status = 'OPERANDO'
                elif runtime.orchestrator.pending_execution:
                    status = 'CONFIRMANDO_ORDEN'
                elif effective_capital < self.settings.min_operating_capital:
                    status = 'INSUFFICIENT_CAPITAL'
                await self._persist_state(
                    runtime, available_equity, effective_capital, status, snapshot,
                    force=bool(state_changed or (result and result.get("filled"))),
                )
            except Exception as exc:
                self.audit.event("USER_RUNTIME_ERROR", runtime.user_id, user_id=runtime.user_id, mode=runtime.mode, symbol=snapshot.symbol, error=str(exc))
                await self._persist_state(runtime, 0.0, 0.0, "ERROR", snapshot)

    async def _persist_state(self, runtime: UserRuntime, available_equity: float, effective_capital: float, status: str, snapshot=None, *, force: bool = False) -> None:
        """Persist dashboard state at a bounded cadence without blocking the market loop.

        Symbol rotation is intentionally *not* allowed to bypass the cadence.  The
        previous implementation included ``last_symbol`` in its signature and could
        therefore write to Mongo on every scanner tick.  Fills/position state changes
        still force an immediate durable update.
        """
        now = time.monotonic()
        interval = float(self.settings.engine_state_persist_seconds)
        if not force and runtime.last_state_persist > 0 and now - runtime.last_state_persist < interval:
            return

        open_obj = next(
            (p for p in runtime.position_manager.positions.values() if p.status == "OPEN"),
            None,
        )
        regime_meta = getattr(runtime.orchestrator.regime_engine, "last_metadata", {}) or {}
        strategy_trace = getattr(runtime.orchestrator.router, "last_trace", {}) or {}
        signature = "|".join([
            str(status),
            str(getattr(open_obj, "position_id", "")),
            str(regime_meta.get("active") or ""),
            str(strategy_trace.get("selected") or ""),
        ])
        open_position = dict(open_obj.__dict__) if open_obj is not None else None

        try:
            positions = await asyncio.to_thread(
                self.db.find_many,
                "positions",
                {"user_id": runtime.user_id, "mode": runtime.mode},
                limit=0,
            )
            period = await asyncio.to_thread(TradingStatistics(self.db).current_period, runtime.mode)
            if period and period.get('global_reset'):
                positions = TradingStatistics(self.db).active_positions(runtime.mode, positions)
            positions = [p for p in positions if not p.get('settlement_pending')]
            baseline = runtime.configured_capital
            account = getattr(self.profiles, 'demo_account', None)
            if runtime.mode == 'demo' and account is not None:
                baseline = await asyncio.to_thread(account.opening_balance, runtime.user_id)
            elif period and period.get('global_reset'):
                baseline = max(0.0, float(available_equity) - sum(
                    position_net_pnl(p) for p in positions if p.get('status') == 'CLOSED'))
            metrics = calculate_performance(positions, baseline)
            metrics['current_capital'] = float(available_equity)
            state_key = {"user_id": runtime.user_id, "mode": runtime.mode.upper()}
            state_doc = {
                "user_id": runtime.user_id,
                "mode": runtime.mode.upper(),
                "status": status,
                "configured_capital": runtime.configured_capital,
                "capital": effective_capital,
                "available_equity": float(available_equity),
                "trading_enabled": runtime.trading_enabled,
                "coinw_connected": runtime.coinw_verified,
                "open_position": open_position,
                "markets_scanned": int(getattr(snapshot, 'markets_scanned', 0) or 0),
                "candidates": int(getattr(snapshot, 'candidates', 0) or 0),
                "last_symbol": getattr(snapshot, 'symbol', None),
                "last_price": getattr(snapshot, 'last', None),
                "last_market_at": getattr(snapshot, 'quote_received_ms', None),
                "last_rejection": getattr(runtime.orchestrator, 'last_rejection', None),
                "regime": regime_meta.get('active'),
                "regime_candidate": regime_meta.get('candidate'),
                "regime_confidence": regime_meta.get('confidence'),
                "strategy": strategy_trace.get('selected'),
                "last_strategy_trace": strategy_trace,
                **metrics,
            }
            await asyncio.to_thread(self.db.upsert, "user_engine_state", state_key, state_doc)
            # Equity is product state too, but it does not need a write on every
            # symbol.  Keep it on the same bounded cadence as engine state.
            if runtime.mode == 'live':
                await asyncio.to_thread(
                    self.profiles.update_available_equity, runtime.user_id, available_equity
                )
            runtime.last_state_persist = now
            runtime.last_state_signature = signature
        except Exception as exc:
            self.audit.event(
                "ENGINE_STATE_PERSIST_ERROR", runtime.user_id, level="ERROR",
                user_id=runtime.user_id, mode=runtime.mode,
                symbol=getattr(snapshot, "symbol", None),
                error=f"{type(exc).__name__}: {exc}",
            )
