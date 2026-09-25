from __future__ import annotations

import time
import math
from app.coinw.normalization import base_quantity

from app.models.enums import Direction
from app.models.trading import Position
from app.position.protection import calculate_break_even_price


class PositionManager:
    """In-memory position state with best-effort persistence.

    Execution/reconciliation is authoritative. Persistence and notification are
    side effects and must never decide whether a real position exists.
    """

    def __init__(self, exit_engine, audit=None, on_realized=None, db=None,
                 evaluate_local_exits=True, owner_user_id=None, owner_mode=None,
                 on_closed=None, on_opened=None, persist_interval_seconds=15.0,
                 estimated_exit_fee_rate=0.0006, break_even_buffer_bps=3.0):
        self.exit_engine = exit_engine
        self.positions: dict[str, Position] = {}
        self.audit = audit
        self.on_realized = on_realized
        self.db = db
        self.evaluate_local_exits = evaluate_local_exits
        self.owner_user_id = owner_user_id
        self.owner_mode = owner_mode
        self.on_closed = on_closed
        self.on_opened = on_opened
        self.persist_interval_seconds = max(1.0, float(persist_interval_seconds))
        self._last_persist: dict[str, float] = {}
        self.persistence = None
        self.exit_slippage_bps = 0.0
        self.estimated_exit_fee_rate = max(0.0, float(estimated_exit_fee_rate))
        self.break_even_buffer_bps = max(0.0, float(break_even_buffer_bps))
        self.state_changed = False

    def add(self, position, *, persist=True):
        if position.remaining_quantity is None:
            position.remaining_quantity = position.quantity
        self._ensure_management_state(position)
        self.positions[position.position_id] = position
        self.state_changed = True
        if persist:
            self._persist(position, force=True)
        return position

    def restore(self, positions):
        for p in positions:
            self.add(p, persist=False)
        self.state_changed = False

    def _ensure_management_state(self, p):
        if getattr(p, "initial_stop_price", None) is None:
            p.initial_stop_price = float(p.stop_price)
        if getattr(p, "structural_target_price", None) is None:
            p.structural_target_price = float(p.target_price)
        if getattr(p, "best_price", None) is None:
            p.best_price = float(p.entry_price)
        if not getattr(p, "management_stage", None):
            p.management_stage = "INITIAL"

        for name, default, low, high in (
            ("break_even_activation_ratio", 0.55, 0.20, 0.90),
            ("profit_lock_activation_ratio", 0.80, 0.40, 0.98),
            ("profit_lock_capture_ratio", 0.35, 0.05, 0.80),
        ):
            try:
                value = float(getattr(p, name, default))
            except (TypeError, ValueError):
                value = default
            setattr(p, name, min(high, max(low, value)))
        if p.profit_lock_activation_ratio <= p.break_even_activation_ratio:
            p.profit_lock_activation_ratio = min(0.98, p.break_even_activation_ratio + 0.15)
        p.profit_lock_capture_ratio = min(
            p.profit_lock_capture_ratio,
            max(0.05, p.profit_lock_activation_ratio - 0.10),
        )

        try:
            configured_fee = float(getattr(p, "estimated_exit_fee_rate", self.estimated_exit_fee_rate))
        except (TypeError, ValueError):
            configured_fee = self.estimated_exit_fee_rate
        p.estimated_exit_fee_rate = configured_fee if configured_fee >= 0 else self.estimated_exit_fee_rate
        try:
            configured_buffer = float(getattr(p, "break_even_buffer_bps", self.break_even_buffer_bps))
        except (TypeError, ValueError):
            configured_buffer = self.break_even_buffer_bps
        p.break_even_buffer_bps = configured_buffer if configured_buffer >= 0 else self.break_even_buffer_bps
        if getattr(p, "break_even_price", None) is None:
            p.break_even_price = calculate_break_even_price(
                p,
                fallback_exit_fee_rate=self.estimated_exit_fee_rate,
                buffer_bps=self.break_even_buffer_bps,
            )

    @staticmethod
    def _management_progress(p) -> float:
        target_distance = abs(float(p.target_price) - float(p.entry_price))
        if target_distance <= 1e-12:
            return 0.0
        best = float(p.best_price if p.best_price is not None else p.entry_price)
        favorable = (best - p.entry_price) if p.direction == Direction.LONG else (p.entry_price - best)
        return max(0.0, favorable / target_distance)

    def _apply_dynamic_protection(self, p, price) -> bool:
        self._ensure_management_state(p)
        if p.direction == Direction.LONG:
            p.best_price = max(float(p.best_price), float(price))
        else:
            p.best_price = min(float(p.best_price), float(price))

        progress = self._management_progress(p)
        target_distance = abs(float(p.target_price) - float(p.entry_price))
        if target_distance <= 1e-12:
            return False

        candidate = None
        stage = None
        if progress >= float(p.profit_lock_activation_ratio):
            lock_distance = target_distance * float(p.profit_lock_capture_ratio)
            candidate = (p.entry_price + lock_distance if p.direction == Direction.LONG
                         else p.entry_price - lock_distance)
            if p.break_even_price is not None:
                candidate = (max(candidate, p.break_even_price) if p.direction == Direction.LONG
                             else min(candidate, p.break_even_price))
            stage = "PROFIT_LOCK"
        elif progress >= float(p.break_even_activation_ratio) and p.break_even_price is not None:
            candidate = float(p.break_even_price)
            stage = "BREAK_EVEN"

        if candidate is None or not math.isfinite(float(candidate)) or float(candidate) <= 0:
            return False

        # Never move a protective stop backwards. The stop may only become more
        # protective as favorable excursion increases.
        epsilon = max(abs(float(p.entry_price)) * 1e-10, 1e-12)
        if p.direction == Direction.LONG:
            candidate = min(float(candidate), float(p.target_price) - epsilon)
            tightened = float(candidate) > float(p.stop_price) + epsilon
        else:
            candidate = max(float(candidate), float(p.target_price) + epsilon)
            tightened = float(candidate) < float(p.stop_price) - epsilon
        if not tightened:
            return False

        previous_stop = float(p.stop_price)
        p.stop_price = float(candidate)
        p.management_stage = stage
        p.stop_moved_to_breakeven = True
        if stage == "PROFIT_LOCK":
            p.profit_lock_price = float(candidate)
        p.protection_update_pending = not self.evaluate_local_exits
        p.revision += 1
        self.state_changed = True
        self._persist(p, force=True)
        if self.audit:
            self.audit.event(
                "PROFIT_LOCK_ACTIVATED" if stage == "PROFIT_LOCK" else "STOP_MOVED_TO_BREAK_EVEN",
                p.decision_id,
                user_id=self.owner_user_id, mode=self.owner_mode,
                position_id=p.position_id, symbol=p.symbol,
                direction=getattr(p.direction, "value", str(p.direction)),
                previous_stop=previous_stop, stop_price=p.stop_price,
                break_even_price=p.break_even_price, best_price=p.best_price,
                progress_ratio=round(progress, 6), target_price=p.target_price,
            )
        return True

    def mark(self, symbol, price, timestamp, bid=None, ask=None, quote_received_ms=None):
        protection_changes = []
        for p in list(self.positions.values()):
            if p.symbol != symbol or p.status != "OPEN":
                continue
            mark_price = (bid if p.direction == Direction.LONG else ask)
            mark_price = price if mark_price is None else mark_price
            if not math.isfinite(float(mark_price)) or float(mark_price) <= 0:
                continue
            price = float(mark_price)
            p.current_price = price
            p.revision += 1
            qty = p.remaining_quantity or p.quantity
            p.unrealized_pnl = self._pnl(p, price, qty)
            if self._apply_dynamic_protection(p, price):
                protection_changes.append(p)
            if self.evaluate_local_exits:
                action = self.exit_engine.evaluate(p, price)
                if action:
                    observed = (quote_received_ms if isinstance(quote_received_ms, (int, float))
                                and 0 < quote_received_ms <= timestamp else timestamp)
                    p.exit_quote_delay_ms = max(0, int(timestamp - observed))
                    self.close_or_reduce(p, action, price, int(observed))
                    continue
            # A mark can arrive every couple of seconds. Persist at a bounded
            # cadence instead of turning Mongo into a per-tick event stream.
            self._persist(p, force=False)
        return protection_changes

    @staticmethod
    def _same_symbol(value: str | None, expected: str | None) -> bool:
        if not expected:
            return True
        left = str(value or "").upper().replace("-", "").replace("_PERP", "").replace("PERP", "")
        right = str(expected or "").upper().replace("-", "").replace("_PERP", "").replace("PERP", "")
        left = left[:-4] if left.endswith("USDT") else left
        right = right[:-4] if right.endswith("USDT") else right
        return left == right

    def reconcile_exchange(self, exchange_positions, timestamp, *, symbol=None,
                           persist=True, notify=True, settlements=None):
        """Reconcile one CoinW symbol without touching positions of other symbols.

        `sync_symbol()` returns only the requested instrument. The former logic
        compared that partial snapshot with *all* local positions, which could
        close a BTC position while the scanner happened to process ETH. The
        optional `symbol` scope makes the exchange snapshot semantics explicit.

        Returns opened/closed/updated positions so the orchestrator can persist
        them asynchronously when desired.
        """
        rows = [
            x for x in (exchange_positions or [])
            if isinstance(x, dict)
            and str(x.get("status", "")).lower() == "open"
            and self._same_symbol(x.get("instrument") or symbol, symbol)
        ]
        exchange = {}
        for row in rows:
            pid = str(row.get("id") or row.get("openId") or "")
            if pid:
                exchange[pid] = row

        local_open = {
            pid: p for pid, p in self.positions.items()
            if p.status == "OPEN" and self._same_symbol(p.symbol, symbol)
        }
        changes = {"opened": [], "closed": [], "updated": []}

        for pid, p in local_open.items():
            row = exchange.get(pid)
            if row is None:
                settlement = (settlements or {}).get(pid)
                if settlements is not None and settlement is None:
                    p.settlement_pending = True
                    p.revision += 1
                    changes['updated'].append(p)
                    continue
                if settlement:
                    p.exit_price = settlement['exit_price']
                    p.net_pnl = settlement['net_pnl']
                    p.closed_at = settlement['closed_at']
                    p.settlement_pending = False
                p.revision += 1
                p.status = "CLOSED"
                p.closed_at = p.closed_at or timestamp
                p.exit_reason = "EXCHANGE_CLOSED"
                p.remaining_quantity = 0.0
                p.unrealized_pnl = 0.0
                self.state_changed = True
                changes["closed"].append(p)
                if self.audit:
                    self.audit.event(
                        "POSITION_CLOSED", p.decision_id,
                        user_id=self.owner_user_id, mode=self.owner_mode,
                        position_id=p.position_id, symbol=p.symbol,
                        direction=getattr(p.direction, "value", str(p.direction)),
                        reason=p.exit_reason, exit_price=p.exit_price,
                        realized_pnl=p.realized_pnl,
                        strategy=getattr(p, "strategy", None),
                        execution_rr=getattr(p, "execution_rr", None),
                        source="exchange_reconcile",
                    )
                if notify:
                    self._call_callback(self.on_closed, p, "POSITION_CLOSE_CALLBACK_ERROR")
                if persist:
                    self._persist(p, force=True)
                continue

            before = (
                p.entry_price, p.stop_price, p.target_price,
                p.quantity, p.remaining_quantity,
            )
            entry = self._float(row.get("openPrice") or row.get("avgPrice"), p.entry_price)
            p.entry_price = entry
            remote_stop = self._float(row.get("stopLossPrice"), p.stop_price)
            local_stop = float(p.stop_price)
            stage = str(getattr(p, "management_stage", "INITIAL") or "INITIAL")
            if stage != "INITIAL":
                # Exchange reconciliation must never loosen a stop already moved
                # to break-even/profit-lock. If the exchange is lagging, keep the
                # tighter local stop and reassert it after reconciliation.
                if p.direction == Direction.LONG:
                    p.stop_price = max(local_stop, remote_stop)
                    remote_is_looser = remote_stop + max(abs(local_stop) * 1e-10, 1e-12) < local_stop
                else:
                    p.stop_price = min(local_stop, remote_stop)
                    remote_is_looser = remote_stop - max(abs(local_stop) * 1e-10, 1e-12) > local_stop
                if remote_is_looser:
                    p.protection_update_pending = True
            else:
                p.stop_price = remote_stop
            p.target_price = self._float(row.get("stopProfitPrice"), p.target_price)
            qty = base_quantity(row, entry)
            p.settlement_pending = False
            if qty > 0:
                p.remaining_quantity = qty
                p.quantity = max(p.quantity, qty)
            after = (
                p.entry_price, p.stop_price, p.target_price,
                p.quantity, p.remaining_quantity,
            )
            if after != before:
                p.revision += 1
                self.state_changed = True
                changes["updated"].append(p)
                if persist:
                    self._persist(p, force=True)

        for pid, row in exchange.items():
            if pid in local_open:
                continue
            entry = self._float(row.get("openPrice") or row.get("avgPrice"), 0.0)
            if entry <= 0:
                continue
            direction = Direction.LONG if str(row.get("direction", "")).lower() == "long" else Direction.SHORT
            qty = base_quantity(row, entry)
            if qty <= 0:
                continue
            recovered = Position(
                position_id=pid,
                decision_id=str(row.get("thirdOrderId") or row.get("decision_id") or "EXCHANGE_RECOVERED"),
                symbol=str(symbol or row.get("instrument") or ""),
                direction=direction,
                quantity=qty,
                entry_price=entry,
                stop_price=self._float(row.get("stopLossPrice"), entry),
                target_price=self._float(row.get("stopProfitPrice"), entry),
                remaining_quantity=qty,
                opened_at=int(row.get("createdDate") or row.get("updatedDate") or timestamp),
                entry_fee=self._float(row.get("fee"), 0.0),
                leverage=int(row.get("leverage") or 10),
            )
            if row.get("strategy") is not None:
                recovered.strategy = row.get("strategy")
            self.add(recovered, persist=False)
            changes["opened"].append(recovered)
            if self.audit:
                self.audit.event(
                    "POSITION_OPENED", recovered.decision_id,
                    user_id=self.owner_user_id, mode=self.owner_mode,
                    position_id=recovered.position_id, symbol=recovered.symbol,
                    direction=getattr(recovered.direction, "value", str(recovered.direction)),
                    quantity=recovered.quantity, entry_price=recovered.entry_price,
                    stop_price=recovered.stop_price, target_price=recovered.target_price,
                    strategy=getattr(recovered, "strategy", None),
                    execution_rr=getattr(recovered, "execution_rr", None),
                    persisted=bool(persist), source="exchange_reconcile",
                )
            if notify:
                self._call_callback(self.on_opened, recovered, "POSITION_OPEN_CALLBACK_ERROR")
            if persist:
                self._persist(recovered, force=True)

        return changes

    def close_or_reduce(self, p, action, price, timestamp):
        trigger_price = price
        if self.evaluate_local_exits:
            slip = self.exit_slippage_bps / 10000
            price *= (1 - slip) if p.direction == Direction.LONG else (1 + slip)
        p.revision += 1
        qty = p.remaining_quantity or p.quantity
        if action == "TP1" and not p.tp1_hit:
            close_qty = qty * 0.5
            gross = self._pnl(p, price, close_qty)
            p.realized_pnl += gross
            p.remaining_quantity = qty - close_qty
            p.tp1_hit = True
            p.stop_price = p.entry_price
            p.stop_moved_to_breakeven = True
            p.unrealized_pnl = self._pnl(p, price, p.remaining_quantity)
            if self.on_realized:
                self.on_realized(p, gross, close_qty, price)
            self._audit("TP1", p, price, close_qty, gross)
            self.state_changed = True
        elif action in ("TP2", "SL"):
            gross = self._pnl(p, price, qty)
            p.realized_pnl += gross
            p.exit_price = price
            p.exit_trigger_price = trigger_price if self.evaluate_local_exits else None
            if action == 'SL' and p.stop_price > 0 and self.evaluate_local_exits:
                adverse = ((p.stop_price - price) if p.direction == Direction.LONG
                           else (price - p.stop_price))
                p.stop_gap_bps = round(max(0.0, adverse / p.stop_price * 10000), 2)
            p.exit_reason = action
            p.closed_at = timestamp
            p.status = "CLOSED"
            p.remaining_quantity = 0
            p.unrealized_pnl = 0.0
            if self.on_realized:
                self.on_realized(p, gross, qty, price)
            self._audit(action, p, price, qty, gross)
            self.state_changed = True
            if self.audit:
                self.audit.event(
                    "POSITION_CLOSED", p.decision_id,
                    user_id=self.owner_user_id, mode=self.owner_mode,
                    position_id=p.position_id, symbol=p.symbol,
                    direction=getattr(p.direction, "value", str(p.direction)),
                    reason=action, exit_price=price, quantity=qty,
                    exit_trigger_price=p.exit_trigger_price,
                    stop_gap_bps=p.stop_gap_bps,
                    quote_delay_ms=p.exit_quote_delay_ms,
                    realized_pnl=p.realized_pnl, gross_pnl=gross,
                    strategy=getattr(p, "strategy", None),
                    execution_rr=getattr(p, "execution_rr", None),
                    source="local_exit",
                )
            self._call_callback(self.on_closed, p, "POSITION_CLOSE_CALLBACK_ERROR")
        self._persist(p, force=True)

    def consume_state_changed(self) -> bool:
        changed = bool(self.state_changed)
        self.state_changed = False
        return changed

    @staticmethod
    def _float(value, default=0.0):
        try:
            return float(value if value is not None else default)
        except (TypeError, ValueError):
            return float(default)

    @staticmethod
    def _pnl(p, price, qty):
        return (price - p.entry_price) * qty if p.direction == Direction.LONG else (p.entry_price - price) * qty

    def _persist(self, p, *, force=False):
        if not self.db:
            return True
        now = time.monotonic()
        previous = self._last_persist.get(p.position_id, 0.0)
        if not force and previous and now - previous < self.persist_interval_seconds:
            return True
        if self.persistence is not None:
            self.persistence.schedule_position_save(position=p, user_id=self.owner_user_id,
                                                    mode=self.owner_mode, symbol=p.symbol)
            self._last_persist[p.position_id] = now
            return True
        document = dict(p.__dict__)
        if self.owner_user_id:
            document["user_id"] = self.owner_user_id
        if self.owner_mode:
            document["mode"] = self.owner_mode
        try:
            self.db.upsert("positions", {"position_id": p.position_id}, document)
            self._last_persist[p.position_id] = now
            return True
        except Exception as exc:
            if self.audit:
                self.audit.event(
                    "POSITION_PERSIST_ERROR", p.decision_id, level="ERROR",
                    user_id=self.owner_user_id, mode=self.owner_mode,
                    symbol=p.symbol, position_id=p.position_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
            return False

    def _call_callback(self, callback, position, error_event):
        if not callback:
            return
        try:
            callback(position)
        except Exception as exc:
            if self.audit:
                self.audit.event(
                    error_event, position.decision_id, level="ERROR",
                    user_id=self.owner_user_id, mode=self.owner_mode,
                    symbol=position.symbol, position_id=position.position_id,
                    error=f"{type(exc).__name__}: {exc}",
                )

    def _audit(self, event, p, price, qty, gross):
        if self.audit:
            self.audit.event(
                event, p.decision_id, user_id=self.owner_user_id,
                mode=self.owner_mode, symbol=p.symbol,
                position_id=p.position_id, price=price,
                quantity=qty, gross_pnl=gross,
            )
