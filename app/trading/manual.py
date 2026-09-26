from __future__ import annotations

import math
import time
from dataclasses import asdict
from uuid import uuid4

from app.coinw.live_adapter import build_live_adapter_from_credentials
from app.coinw.normalization import base_quantity
from app.coinw.market import CoinWMarketClient
from app.execution.demo import DemoExecutionEngine
from app.market.chart_service import ChartMarketService, canonical_symbol
from app.models.enums import Direction, Strategy
from app.models.trading import Position, TradeIntent
from app.trading.metrics import position_net_pnl


class ManualTradingError(ValueError):
    pass


def _f(value, default=0.0) -> float:
    try:
        n = float(value if value is not None else default)
        return n if math.isfinite(n) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _rows(payload):
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("rows", "data", "list", "items"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _data(payload):
    return payload.get("data", payload) if isinstance(payload, dict) else payload


def _order_id(payload) -> str | None:
    data = _data(payload)
    if isinstance(data, dict):
        value = data.get("value") or data.get("id") or data.get("orderId")
    else:
        value = data
    return str(value) if value not in (None, "") else None


def _position_margin(row: dict) -> float:
    explicit = _f(row.get("position_margin") or row.get("positionMargin") or row.get("margin"))
    if explicit > 0:
        return explicit
    entry = _f(row.get("entry_price") or row.get("openPrice"))
    qty = _f(row.get("remaining_quantity") or row.get("quantity"))
    leverage = max(1.0, _f(row.get("leverage"), 1.0))
    return max(0.0, entry * qty / leverage)


def manual_position_from_exchange(order: dict, row: dict) -> Position | None:
    entry = _f(row.get("openPrice") or row.get("avgPrice") or row.get("orderPrice") or order.get("limit_price"))
    if entry <= 0:
        return None
    quantity = base_quantity(row, entry)
    if quantity <= 0:
        return None
    direction = Direction.LONG if str(row.get("direction") or order.get("direction") or order.get("side")).lower() == "long" else Direction.SHORT
    position_id = str(row.get("id") or row.get("openId") or "")
    if not position_id:
        return None
    leverage = int(row.get("leverage") or order.get("leverage") or 1)
    p = Position(
        position_id=position_id,
        decision_id=str(order.get("client_order_id") or "MANUAL"),
        symbol=str(order.get("symbol") or row.get("instrument") or ""),
        direction=direction,
        quantity=quantity,
        entry_price=entry,
        stop_price=_f(row.get("stopLossPrice"), order.get("stop_price")),
        target_price=_f(row.get("stopProfitPrice"), order.get("target_price")),
        remaining_quantity=quantity,
        opened_at=int(row.get("createdDate") or row.get("updatedDate") or int(time.time() * 1000)),
        entry_fee=_f(row.get("fee")),
        leverage=leverage,
        source="MANUAL",
        order_type=str(order.get("order_type") or "MARKET").upper(),
        margin_mode="ISOLATED" if int(row.get("positionModel") or 0) == 0 else "CROSS",
        position_margin=_f(row.get("positionMargin") or row.get("margin"), order.get("margin")) or None,
        liquidation_price=_f(row.get("liquidationPrice")) or None,
        exchange_order_id=str(order.get("order_id") or "") or None,
        client_order_id=str(order.get("client_order_id") or "") or None,
        current_price=_f(row.get("currentPrice") or row.get("markPrice"), entry),
        unrealized_pnl=_f(row.get("profitUnreal")),
    )
    p.tp2_price = p.target_price
    p.strategy = "MANUAL"
    return p


def position_from_document(row: dict) -> Position:
    direction = row.get("direction")
    if not isinstance(direction, Direction):
        direction = Direction(str(direction or "LONG").upper())
    p = Position(
        position_id=str(row["position_id"]),
        decision_id=str(row.get("decision_id") or row.get("client_order_id") or "MANUAL"),
        symbol=str(row.get("symbol") or ""),
        direction=direction,
        quantity=_f(row.get("quantity")),
        entry_price=_f(row.get("entry_price")),
        stop_price=_f(row.get("stop_price")),
        target_price=_f(row.get("target_price")),
        status=str(row.get("status") or "OPEN"),
        realized_pnl=_f(row.get("realized_pnl")),
        unrealized_pnl=_f(row.get("unrealized_pnl")),
        tp1_price=row.get("tp1_price"),
        tp2_price=row.get("tp2_price"),
        remaining_quantity=row.get("remaining_quantity"),
        tp1_hit=bool(row.get("tp1_hit", False)),
        stop_moved_to_breakeven=bool(row.get("stop_moved_to_breakeven", False)),
        entry_fee=_f(row.get("entry_fee")),
        exit_fee=_f(row.get("exit_fee")),
        funding_pnl=_f(row.get("funding_pnl")),
        revision=int(row.get("revision") or 0),
        current_price=row.get("current_price"),
        settlement_pending=bool(row.get("settlement_pending", False)),
        net_pnl=row.get("net_pnl"),
        leverage=int(row.get("leverage") or 1),
        protected=bool(row.get("protected", True)),
        initial_stop_price=row.get("initial_stop_price"),
        structural_target_price=row.get("structural_target_price"),
        target_front_run_ratio=row.get("target_front_run_ratio"),
        break_even_price=row.get("break_even_price"),
        break_even_activation_ratio=_f(row.get("break_even_activation_ratio"), 0.55),
        profit_lock_activation_ratio=_f(row.get("profit_lock_activation_ratio"), 0.80),
        profit_lock_capture_ratio=_f(row.get("profit_lock_capture_ratio"), 0.35),
        profit_lock_price=row.get("profit_lock_price"),
        management_stage=str(row.get("management_stage") or "INITIAL"),
        best_price=row.get("best_price"),
        estimated_exit_fee_rate=_f(row.get("estimated_exit_fee_rate"), 0.0006),
        break_even_buffer_bps=_f(row.get("break_even_buffer_bps"), 3.0),
        protection_update_pending=bool(row.get("protection_update_pending", False)),
        exit_trigger_price=row.get("exit_trigger_price"),
        stop_gap_bps=row.get("stop_gap_bps"),
        exit_quote_delay_ms=row.get("exit_quote_delay_ms"),
        source=str(row.get("source") or "BOT").upper(),
        order_type=str(row.get("order_type") or "MARKET").upper(),
        margin_mode=str(row.get("margin_mode") or "ISOLATED").upper(),
        position_margin=row.get("position_margin"),
        liquidation_price=row.get("liquidation_price"),
        exchange_order_id=row.get("exchange_order_id"),
        client_order_id=row.get("client_order_id"),
        opened_at=row.get("opened_at"),
        closed_at=row.get("closed_at"),
        exit_price=row.get("exit_price"),
        exit_reason=row.get("exit_reason"),
    )
    for attr in ("strategy", "quality", "execution_rr", "structural_rr", "close_requested", "close_order_id"):
        if row.get(attr) is not None:
            setattr(p, attr, row.get(attr))
    return p


class ManualTradingService:
    def __init__(self, settings, db, profiles, billing, audit=None):
        self.settings = settings
        self.db = db
        self.profiles = profiles
        self.billing = billing
        self.audit = audit
        self.market = ChartMarketService(CoinWMarketClient(base_url=settings.coinw_rest_base_url))

    def _profile(self, user_id: str):
        return self.profiles.public(user_id)

    def _assert_enabled(self):
        if not self.settings.manual_trading_enabled:
            raise ManualTradingError("manual_trading_disabled")

    def _assert_active_mode(self, profile, requested_mode: str):
        active = str(profile.execution_mode).lower()
        if str(requested_mode).lower() != active:
            raise ManualTradingError("manual_mode_must_match_active_mode")
        return active

    def _assert_live_access(self, user_id: str, profile, confirmed: bool):
        if not self.billing.entitlement(user_id).live_allowed:
            raise ManualTradingError("live_not_entitled")
        if not profile.coinw_configured or not profile.coinw_verified:
            raise ManualTradingError("coinw_verification_required_for_live_manual")
        if self.settings.manual_live_confirmation_required and not confirmed:
            raise ManualTradingError("live_manual_confirmation_required")

    def _validate_order(self, *, side: str, margin: float, leverage: int, order_type: str,
                        reference_price: float, stop_loss: float, take_profit: float,
                        limit_price: float | None):
        side = str(side).upper()
        if side not in {"LONG", "SHORT"}:
            raise ManualTradingError("invalid_manual_side")
        if not math.isfinite(margin) or margin < self.settings.manual_trading_min_margin:
            raise ManualTradingError("manual_margin_below_minimum")
        if leverage < 1 or leverage > self.settings.manual_trading_max_leverage:
            raise ManualTradingError("manual_leverage_out_of_range")
        order_type = str(order_type).upper()
        if order_type not in {"MARKET", "LIMIT"}:
            raise ManualTradingError("unsupported_manual_order_type")
        entry = float(limit_price) if order_type == "LIMIT" else float(reference_price)
        if not math.isfinite(entry) or entry <= 0:
            raise ManualTradingError("invalid_manual_entry_price")
        if order_type == "LIMIT" and (limit_price is None or not math.isfinite(float(limit_price)) or float(limit_price) <= 0):
            raise ManualTradingError("manual_limit_price_required")
        if not math.isfinite(stop_loss) or not math.isfinite(take_profit) or stop_loss <= 0 or take_profit <= 0:
            raise ManualTradingError("manual_tp_sl_required")
        if side == "LONG" and not (stop_loss < entry < take_profit):
            raise ManualTradingError("invalid_manual_long_geometry")
        if side == "SHORT" and not (take_profit < entry < stop_loss):
            raise ManualTradingError("invalid_manual_short_geometry")
        return Direction.LONG if side == "LONG" else Direction.SHORT, entry

    def _demo_account(self, user_id: str) -> dict:
        balance = float(self.profiles.demo_account.balance(user_id))
        opened = self.db.find_many("positions", {"user_id": user_id, "mode": "demo", "status": "OPEN"}, limit=0)
        used_margin = sum(_position_margin(row) for row in opened)
        pending = self.db.find_many("manual_orders", {"user_id": user_id, "mode": "demo", "status": "OPEN"}, limit=0)
        reserved = sum(max(0.0, _f(row.get("margin"))) for row in pending)
        unrealized = sum(_f(row.get("unrealized_pnl")) for row in opened)
        return {
            "balance": balance,
            "available": max(0.0, balance - used_margin - reserved),
            "used_margin": used_margin,
            "frozen_margin": reserved,
            "unrealized_pnl": unrealized,
            "equity": balance + unrealized,
        }

    async def _live_adapter(self, user_id: str):
        creds = self.profiles.credentials(user_id)
        return build_live_adapter_from_credentials(
            creds.api_key, creds.api_secret,
            base_url=self.settings.coinw_rest_base_url,
            audit=self.audit,
        )

    async def _live_account(self, user_id: str) -> dict:
        adapter = await self._live_adapter(user_id)
        response = await adapter.account.assets("usdt")
        data = _data(response)
        data = data if isinstance(data, dict) else {}
        available = _f(data.get("availableUsdt"))
        used = _f(data.get("alMargin"))
        frozen = _f(data.get("alFreeze"))
        unrealized = 0.0
        for row in self.db.find_many("positions", {"user_id": user_id, "mode": "live", "status": "OPEN"}, limit=0):
            unrealized += _f(row.get("unrealized_pnl"))
        return {
            "balance": available + used + frozen,
            "available": available,
            "used_margin": used,
            "frozen_margin": frozen,
            "unrealized_pnl": unrealized,
            "equity": available + used + frozen + unrealized,
            "exchange_available_margin": _f(data.get("availableMargin")),
            "updated_at": data.get("time") or data.get("ts"),
        }

    async def state(self, user_id: str) -> dict:
        self._assert_enabled()
        profile = self._profile(user_id)
        mode = str(profile.execution_mode).lower()
        account = self._demo_account(user_id) if mode == "demo" else await self._live_account(user_id)
        positions = self.db.find_many("positions", {"user_id": user_id, "mode": mode, "status": "OPEN"}, limit=0, sort_field="opened_at")
        closed_manual = [
            row for row in self.db.find_many(
                "positions", {"user_id": user_id, "mode": mode, "status": "CLOSED"},
                limit=100, sort_field="closed_at",
            )
            if str(row.get("source") or "BOT").upper() == "MANUAL"
        ][:50]
        orders = self.db.find_many("manual_orders", {"user_id": user_id, "mode": mode}, limit=100, sort_field="created_at")

        if mode == "live" and profile.coinw_verified:
            adapter = await self._live_adapter(user_id)
            by_symbol: dict[str, list[dict]] = {}
            for row in positions:
                by_symbol.setdefault(str(row.get("symbol") or ""), []).append(row)
            for symbol, local_rows in by_symbol.items():
                if not symbol:
                    continue
                try:
                    remote_rows = await adapter.current_position_rows(symbol)
                except Exception:
                    continue
                remote = {str(r.get("id") or r.get("openId")): r for r in remote_rows if str(r.get("status", "")).lower() == "open"}
                for local in local_rows:
                    r = remote.get(str(local.get("position_id")))
                    if not r:
                        continue
                    local["entry_price"] = _f(r.get("openPrice") or r.get("avgPrice"), local.get("entry_price"))
                    local["unrealized_pnl"] = _f(r.get("profitUnreal"), local.get("unrealized_pnl"))
                    local["liquidation_price"] = _f(r.get("liquidationPrice")) or None
                    local["position_margin"] = _f(r.get("positionMargin") or r.get("margin")) or local.get("position_margin")
                    local["stop_price"] = _f(r.get("stopLossPrice"), local.get("stop_price"))
                    local["target_price"] = _f(r.get("stopProfitPrice"), local.get("target_price"))
                    local["leverage"] = int(r.get("leverage") or local.get("leverage") or 1)
                    local["exchange_synced"] = True
                    self.db.upsert("positions", {"position_id": local["position_id"]}, local)

            # Reconcile LIVE orders. CoinW accepting an orderId is not the same as
            # a fill. The current-order endpoint is documented for unfulfilled
            # orders, so after a fill we fall back to 7-day order history and use
            # thirdOrderId/openId to bind the exact CoinW position to this manual
            # request.
            pending_statuses = {"OPEN", "ACCEPTED", "PENDING", "SUBMITTING", "UNKNOWN", "PARTIALLY_FILLED"}
            for order in [o for o in orders if str(o.get("status", "")).upper() in pending_statuses]:
                try:
                    position_type = "plan" if str(order.get("order_type")).upper() == "LIMIT" else "execute"
                    instrument = adapter._instrument(str(order.get("symbol") or ""))
                    client_id = str(order.get("client_order_id") or "")
                    exchange_order_id = order.get("order_id")
                    order_row = None

                    if exchange_order_id:
                        info = await adapter.orders.information(
                            [exchange_order_id], position_type=position_type,
                        )
                        found = _rows(info)
                        order_row = found[0] if found else None
                    else:
                        # UNKNOWN can mean CoinW accepted the request but the HTTP
                        # response was lost before we received the exchange id.
                        # Recover it by thirdOrderId; never resubmit the order.
                        try:
                            current = await adapter.orders.open_orders(
                                instrument, position_type=position_type
                            )
                            current_rows = _rows(current)
                            order_row = next((
                                row for row in current_rows
                                if str(row.get("thirdOrderId") or "") == client_id
                            ), None)
                        except Exception:
                            order_row = None

                    # Filled orders can disappear from /v1/perpum/order. Resolve
                    # them from history using our client id, which CoinW returns
                    # as thirdOrderId, and obtain the resulting openId.
                    need_history = order_row is None or str(
                        (order_row or {}).get("orderStatus") or ""
                    ).lower() in {"finish", "part", "filled"}
                    if need_history and hasattr(adapter.orders, "history"):
                        history = await adapter.orders.history(
                            instrument, position_type, page=1, page_size=50,
                        )
                        history_rows = _rows(history)
                        historical = next((
                            row for row in history_rows
                            if str(row.get("thirdOrderId") or "") == client_id
                        ), None)
                        if historical is None and exchange_order_id:
                            historical = next((
                                row for row in history_rows
                                if str(row.get("id") or row.get("orderId") or "") == str(exchange_order_id)
                            ), None)
                        if historical is not None:
                            order_row = historical

                    if order_row is None:
                        order["reconcile_required"] = True
                        order["last_reconcile_at"] = int(time.time() * 1000)
                        self.db.upsert("manual_orders", {"manual_order_id": order["manual_order_id"]}, order)
                        continue

                    recovered_order_id = order_row.get("id") or order_row.get("orderId")
                    if not exchange_order_id and recovered_order_id not in (None, ""):
                        order["order_id"] = str(recovered_order_id)
                        order["reconcile_required"] = False
                    exchange_status = str(
                        order_row.get("orderStatus")
                        or order_row.get("finalOrderStatus")
                        or order_row.get("status")
                        or "OPEN"
                    ).upper()
                    order["exchange_status"] = exchange_status
                    normalized = exchange_status.lower()
                    if normalized in {"cancel", "cancelled", "reject", "rejected"}:
                        order["status"] = "CANCELLED" if normalized.startswith("cancel") else "REJECTED"
                    elif normalized in {"unfinish", "unfinished", "open", "accepted", "pending"}:
                        # Once the exchange order is recovered we no longer need
                        # to expose an ambiguous UNKNOWN state locally.
                        order["status"] = "OPEN"
                    elif normalized in {"finish", "filled", "part", "partial", "partially_filled"}:
                        open_id = order_row.get("openId")
                        remote_rows = await adapter.current_position_rows(
                            str(order.get("symbol") or ""),
                            [open_id] if open_id not in (None, "") else None,
                        )
                        direction = str(order.get("direction") or order.get("side") or "").lower()
                        candidates = [
                            r for r in remote_rows
                            if str(r.get("status", "")).lower() == "open"
                            and str(r.get("direction", "")).lower() == direction
                        ]
                        remote = None
                        if open_id not in (None, ""):
                            remote = next((
                                r for r in candidates
                                if str(r.get("id") or r.get("openId") or "") == str(open_id)
                            ), None)
                        elif len(candidates) == 1:
                            # Never attach a manual order to an arbitrary position
                            # when multiple same-direction positions exist.
                            remote = candidates[0]
                        if remote:
                            position = manual_position_from_exchange(order, remote)
                            if position is not None:
                                existing_position = self.db.find_one(
                                    "positions", {"position_id": position.position_id}
                                )
                                if existing_position:
                                    position.revision = max(
                                        int(position.revision or 0),
                                        int(existing_position.get("revision") or 0) + 1,
                                    )
                                position_doc = {**asdict(position), "strategy": "MANUAL", "user_id": user_id, "mode": "live"}
                                self.db.upsert("positions", {"position_id": position.position_id}, position_doc)
                                replaced = False
                                for index, local in enumerate(positions):
                                    if str(local.get("position_id")) == position.position_id:
                                        positions[index] = position_doc
                                        replaced = True
                                        break
                                if not replaced:
                                    positions.append(position_doc)
                                order["position_id"] = position.position_id
                                order["fill_price"] = position.entry_price
                                order["filled"] = True
                        order["status"] = "PARTIALLY_FILLED" if normalized in {"part", "partial", "partially_filled"} else "FILLED"
                        if order["status"] == "FILLED":
                            order["filled_at"] = int(time.time() * 1000)
                    self.db.upsert("manual_orders", {"manual_order_id": order["manual_order_id"]}, order)
                except Exception:
                    # Keep the last known state. A transient reconciliation error
                    # must never trigger an automatic resubmission.
                    pass

        # Position-level unrealized PnL is refreshed from CoinW above; keep the
        # account summary consistent with those same rows in this response.
        if mode == "live":
            account["unrealized_pnl"] = sum(_f(row.get("unrealized_pnl")) for row in positions)
            account["equity"] = _f(account.get("balance")) + account["unrealized_pnl"]

        clean_positions = [{k: v for k, v in row.items() if k != "_id"} for row in positions]
        clean_closed_manual = [{k: v for k, v in row.items() if k != "_id"} for row in closed_manual]
        clean_orders = [{k: v for k, v in row.items() if k != "_id"} for row in orders]
        return {
            "mode": mode,
            "enabled": True,
            "account": account,
            "positions": clean_positions,
            "position_history": clean_closed_manual,
            "open_orders": [o for o in clean_orders if str(o.get("status", "")).upper() in {"OPEN", "ACCEPTED", "PENDING", "SUBMITTING", "UNKNOWN", "PARTIALLY_FILLED"}],
            "order_history": [o for o in clean_orders if str(o.get("status", "")).upper() not in {"OPEN", "ACCEPTED", "PENDING", "SUBMITTING", "UNKNOWN", "PARTIALLY_FILLED"}][:50],
            "min_margin": self.settings.manual_trading_min_margin,
            "max_leverage": self.settings.manual_trading_max_leverage,
            "live_confirmation_required": self.settings.manual_live_confirmation_required,
        }

    async def place_order(self, user_id: str, request) -> dict:
        self._assert_enabled()
        profile = self._profile(user_id)
        mode = self._assert_active_mode(profile, request.mode)
        existing = self.db.find_one("manual_orders", {"user_id": user_id, "client_order_id": request.client_order_id})
        if existing:
            return {"idempotent_replay": True, **{k: v for k, v in existing.items() if k != "_id"}}
        if mode == "live":
            self._assert_live_access(user_id, profile, bool(request.confirm_live))

        symbol = canonical_symbol(request.symbol)
        snapshot = await self.market.snapshot(symbol)
        asks = snapshot.get("order_book", {}).get("asks") or []
        bids = snapshot.get("order_book", {}).get("bids") or []
        last = _f(snapshot.get("ticker", {}).get("last"))
        best_ask = _f(asks[0].get("price") if asks else None, last)
        best_bid = _f(bids[0].get("price") if bids else None, last)
        reference = best_ask if str(request.side).upper() == "LONG" else best_bid
        direction, entry_reference = self._validate_order(
            side=request.side, margin=float(request.margin), leverage=int(request.leverage),
            order_type=request.order_type, reference_price=reference,
            stop_loss=float(request.stop_loss), take_profit=float(request.take_profit),
            limit_price=request.limit_price,
        )
        order_type = str(request.order_type).upper()
        # Balance checks are read-only and happen before the idempotency claim so
        # validation failures do not leave a phantom SUBMITTING order.
        account = self._demo_account(user_id) if mode == "demo" else await self._live_account(user_id)
        if float(request.margin) > account["available"] + 1e-9:
            raise ManualTradingError("manual_margin_exceeds_available_balance")

        manual_order_id = f"MANUAL-{uuid4().hex[:20]}"
        now_ms = int(time.time() * 1000)
        base_doc = {
            "manual_order_id": manual_order_id,
            "client_order_id": request.client_order_id,
            "user_id": user_id,
            "mode": mode,
            "source": "MANUAL",
            "symbol": symbol,
            "side": direction.value,
            "direction": direction.value,
            "order_type": order_type,
            "margin": float(request.margin),
            "leverage": int(request.leverage),
            "notional": float(request.margin) * int(request.leverage),
            "limit_price": float(request.limit_price) if request.limit_price is not None else None,
            "stop_price": float(request.stop_loss),
            "target_price": float(request.take_profit),
            "margin_mode": "ISOLATED",
            "created_at": now_ms,
        }

        # Claim the client id before any order side effect. This is the financial
        # idempotency boundary: two simultaneous taps/requests cannot both reach
        # CoinW. If a network failure later leaves the outcome uncertain, retries
        # return this same record rather than submitting a second order.
        claimed = self.db.set_once(
            "manual_orders",
            {"user_id": user_id, "client_order_id": request.client_order_id},
            {**base_doc, "status": "SUBMITTING", "accepted": False, "filled": False},
        )
        if str(claimed.get("manual_order_id")) != manual_order_id:
            return {"idempotent_replay": True, **{k: v for k, v in claimed.items() if k != "_id"}}

        if mode == "demo":
            if order_type == "LIMIT":
                doc = {**base_doc, "status": "OPEN", "entry_reference": entry_reference}
                self.db.upsert("manual_orders", {"manual_order_id": manual_order_id}, doc)
                return {**doc, "accepted": True, "filled": False}

            engine = DemoExecutionEngine(
                audit=self.audit,
                fee_rate=self.settings.paper_taker_fee,
                slippage_bps=self.settings.paper_slippage_bps,
                max_spread_bps=self.settings.paper_max_spread_bps,
                initial_equity=max(account["available"], float(request.margin)),
                leverage=int(request.leverage),
            )
            intent = TradeIntent(
                decision_id=request.client_order_id, symbol=symbol, strategy=Strategy.NO_TRADE,
                direction=direction, entry_price=reference,
                stop_price=float(request.stop_loss), target_price=float(request.take_profit),
                quality=100.0, risk_multiplier=1.0, timeframe="manual",
                reasons=("manual_demo_order",), metadata={"source": "MANUAL"},
            )
            result = engine.submit(intent, float(request.margin) * int(request.leverage), {
                "bid": best_bid, "ask": best_ask, "ts": now_ms,
            })
            if not result.get("filled"):
                reason = str(result.get("reason") or "manual_demo_order_rejected")
                self.db.upsert(
                    "manual_orders", {"manual_order_id": manual_order_id},
                    {**base_doc, "status": "REJECTED", "accepted": False, "filled": False, "reject_reason": reason},
                )
                raise ManualTradingError(reason)
            position: Position = result["position"]
            position.source = "MANUAL"
            position.order_type = "MARKET"
            position.margin_mode = "ISOLATED"
            position.position_margin = float(request.margin)
            position.client_order_id = request.client_order_id
            position.exchange_order_id = result.get("order_id") or position.position_id
            position.strategy = "MANUAL"
            position.leverage = int(request.leverage)
            self.db.upsert("positions", {"position_id": position.position_id}, {
                **asdict(position), "strategy": "MANUAL", "user_id": user_id, "mode": mode,
            })
            doc = {**base_doc, "status": "FILLED", "filled": True, "accepted": True,
                   "position_id": position.position_id, "order_id": position.exchange_order_id,
                   "fill_price": position.entry_price, "filled_at": now_ms}
            self.db.upsert("manual_orders", {"manual_order_id": manual_order_id}, doc)
            return {**doc, "position": asdict(position)}

        adapter = await self._live_adapter(user_id)
        try:
            result = await adapter.submit_manual(
                client_order_id=request.client_order_id,
                symbol=symbol,
                direction=direction,
                margin=float(request.margin), leverage=int(request.leverage),
                order_type=order_type,
                stop_loss=float(request.stop_loss), take_profit=float(request.take_profit),
                limit_price=request.limit_price, reference_price=reference,
                position_model=0,
            )
        except Exception:
            self.db.upsert(
                "manual_orders", {"manual_order_id": manual_order_id},
                {**base_doc, "status": "UNKNOWN", "accepted": False, "filled": False,
                 "reconcile_required": True, "updated_at_ms": int(time.time() * 1000)},
            )
            raise
        if not result.get("accepted"):
            reason = str(result.get("reason") or "coinw_manual_order_rejected")
            self.db.upsert(
                "manual_orders", {"manual_order_id": manual_order_id},
                {**base_doc, "status": "REJECTED", "accepted": False, "filled": False, "reject_reason": reason},
            )
            raise ManualTradingError(reason)
        position = result.get("position")
        if isinstance(position, Position):
            position.strategy = "MANUAL"
            self.db.upsert("positions", {"position_id": position.position_id}, {
                **asdict(position), "strategy": "MANUAL", "user_id": user_id, "mode": mode,
            })
        doc = {
            **base_doc,
            "order_id": result.get("order_id"),
            "position_id": result.get("position_id"),
            "status": "FILLED" if result.get("filled") else str(result.get("status") or "ACCEPTED").upper(),
            "accepted": True,
            "filled": bool(result.get("filled")),
            "fill_price": result.get("fill_price"),
            "exchange_status": result.get("status"),
            "protected": result.get("protected"),
            "protection_error": result.get("error"),
            "filled_at": now_ms if result.get("filled") else None,
        }
        self.db.upsert("manual_orders", {"manual_order_id": manual_order_id}, doc)
        return {**doc, "position": asdict(position) if isinstance(position, Position) else None}

    async def cancel_order(self, user_id: str, manual_order_id: str, *, confirm_live: bool) -> dict:
        self._assert_enabled()
        order = self.db.find_one("manual_orders", {"manual_order_id": manual_order_id, "user_id": user_id})
        if not order:
            raise ManualTradingError("manual_order_not_found")
        if str(order.get("status", "")).upper() not in {"OPEN", "ACCEPTED", "PENDING"}:
            raise ManualTradingError("manual_order_not_cancellable")
        mode = str(order.get("mode") or "demo")
        if mode == "live":
            profile = self._profile(user_id)
            self._assert_live_access(user_id, profile, confirm_live)
            exchange_id = order.get("order_id")
            if not exchange_id:
                raise ManualTradingError("manual_exchange_order_id_missing")
            adapter = await self._live_adapter(user_id)
            await adapter.orders.cancel(exchange_id)
        update = {**order, "status": "CANCELLED", "cancelled_at": int(time.time() * 1000)}
        self.db.upsert("manual_orders", {"manual_order_id": manual_order_id}, update)
        return {k: v for k, v in update.items() if k != "_id"}

    async def close_position(self, user_id: str, position_id: str, *, confirm_live: bool) -> dict:
        self._assert_enabled()
        row = self.db.find_one("positions", {"position_id": position_id, "user_id": user_id, "status": "OPEN"})
        if not row:
            raise ManualTradingError("manual_position_not_found")
        if str(row.get("source") or "BOT").upper() != "MANUAL":
            raise ManualTradingError("only_manual_positions_can_be_closed_here")
        mode = str(row.get("mode") or "demo")
        if mode == "live":
            profile = self._profile(user_id)
            self._assert_live_access(user_id, profile, confirm_live)
            adapter = await self._live_adapter(user_id)
            response = await adapter.positions.close(position_id, close_rate=1, position_type="execute")
            close_order_id = _order_id(response)
            update = {
                **row,
                "settlement_pending": True,
                "close_requested": True,
                "close_order_id": close_order_id,
                "close_requested_at": int(time.time() * 1000),
            }
            self.db.upsert("positions", {"position_id": position_id}, update)
            return {"accepted": True, "pending": True, "close_order_id": close_order_id, "position_id": position_id}

        snapshot = await self.market.snapshot(str(row.get("symbol")))
        asks = snapshot.get("order_book", {}).get("asks") or []
        bids = snapshot.get("order_book", {}).get("bids") or []
        last = _f(snapshot.get("ticker", {}).get("last"), row.get("current_price") or row.get("entry_price"))
        direction = Direction(str(row.get("direction") or "LONG").upper())
        raw = _f(bids[0].get("price") if direction == Direction.LONG and bids else asks[0].get("price") if asks else None, last)
        slip = self.settings.paper_slippage_bps / 10000.0
        fill = raw * (1 - slip) if direction == Direction.LONG else raw * (1 + slip)
        qty = _f(row.get("remaining_quantity") or row.get("quantity"))
        gross = (fill - _f(row.get("entry_price"))) * qty if direction == Direction.LONG else (_f(row.get("entry_price")) - fill) * qty
        exit_fee = fill * qty * self.settings.paper_taker_fee
        update = {
            **row,
            "status": "CLOSED",
            "realized_pnl": _f(row.get("realized_pnl")) + gross,
            "exit_fee": _f(row.get("exit_fee")) + exit_fee,
            "unrealized_pnl": 0.0,
            "remaining_quantity": 0.0,
            "exit_price": fill,
            "current_price": fill,
            "exit_reason": "MANUAL_CLOSE",
            "closed_at": int(time.time() * 1000),
            "settlement_pending": False,
            "revision": int(row.get("revision") or 0) + 1,
        }
        update["net_pnl"] = position_net_pnl(update)
        self.db.upsert("positions", {"position_id": position_id}, update)
        return {"accepted": True, "pending": False, "position": {k: v for k, v in update.items() if k != "_id"}}

    async def update_protection(self, user_id: str, position_id: str, *, stop_loss: float,
                                take_profit: float, confirm_live: bool) -> dict:
        self._assert_enabled()
        row = self.db.find_one("positions", {"position_id": position_id, "user_id": user_id, "status": "OPEN"})
        if not row:
            raise ManualTradingError("manual_position_not_found")
        if str(row.get("source") or "BOT").upper() != "MANUAL":
            raise ManualTradingError("only_manual_positions_can_be_modified_here")
        direction = Direction(str(row.get("direction") or "LONG").upper())
        current = _f(row.get("current_price") or row.get("entry_price"))
        if direction == Direction.LONG and not (stop_loss < current < take_profit):
            raise ManualTradingError("invalid_manual_long_protection")
        if direction == Direction.SHORT and not (take_profit < current < stop_loss):
            raise ManualTradingError("invalid_manual_short_protection")
        mode = str(row.get("mode") or "demo")
        if mode == "live":
            profile = self._profile(user_id)
            self._assert_live_access(user_id, profile, confirm_live)
            adapter = await self._live_adapter(user_id)
            await adapter.orders.set_tpsl(
                position_id, adapter._instrument(str(row.get("symbol"))),
                stop_loss=stop_loss, take_profit=take_profit,
            )
        update = {
            **row,
            "stop_price": float(stop_loss),
            "target_price": float(take_profit),
            "tp2_price": float(take_profit),
            "protected": True,
            "revision": int(row.get("revision") or 0) + 1,
        }
        self.db.upsert("positions", {"position_id": position_id}, update)
        return {"updated": True, "position": {k: v for k, v in update.items() if k != "_id"}}


def build_demo_limit_fill(order: dict, fill_price: float, settings) -> Position:
    direction = Direction(str(order.get("direction") or order.get("side") or "LONG").upper())
    notional = _f(order.get("notional"))
    leverage = int(order.get("leverage") or 1)
    qty = notional / max(fill_price, 1e-12)
    fee = notional * settings.paper_maker_fee
    p = Position(
        position_id=f"PAPER-{uuid4().hex[:16]}",
        decision_id=str(order.get("client_order_id") or order.get("manual_order_id") or "MANUAL"),
        symbol=str(order.get("symbol") or ""),
        direction=direction,
        quantity=qty,
        entry_price=fill_price,
        stop_price=_f(order.get("stop_price")),
        target_price=_f(order.get("target_price")),
        entry_fee=fee,
        opened_at=int(time.time() * 1000),
        leverage=leverage,
        source="MANUAL",
        order_type="LIMIT",
        margin_mode="ISOLATED",
        position_margin=_f(order.get("margin")),
        exchange_order_id=str(order.get("manual_order_id")),
        client_order_id=str(order.get("client_order_id")),
        current_price=fill_price,
    )
    p.tp2_price = p.target_price
    p.remaining_quantity = qty
    p.strategy = "MANUAL"
    return p
