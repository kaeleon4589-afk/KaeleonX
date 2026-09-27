from __future__ import annotations

import asyncio
from uuid import uuid4

from app.models.trading import Position
from app.models.enums import Direction
from app.coinw.normalization import base_quantity
from app.position.protection import apply_intent_management


class CoinWExecutor:
    """Live adapter with explicit order confirmation and exchange-side protection."""

    def __init__(self, orders_api, positions_api, account_api=None,
                 audit=None, enabled=False, confirm_attempts=5,
                 confirm_delay=0.5):
        self.orders = orders_api
        self.positions = positions_api
        self.account = account_api
        self.audit = audit
        self.enabled = enabled
        self.confirm_attempts = confirm_attempts
        self.confirm_delay = confirm_delay
        self._instrument_cache = {}

    def _instrument(self, symbol):
        return symbol.upper().replace("USDT", "")

    @staticmethod
    def _round_price(value, precision, mode="nearest"):
        try:
            numeric = float(value)
            digits = max(0, int(precision))
        except (TypeError, ValueError):
            return float(value)
        factor = 10 ** digits
        if mode == "up":
            return math.ceil(numeric * factor - 1e-12) / factor
        if mode == "down":
            return math.floor(numeric * factor + 1e-12) / factor
        return round(numeric, digits)

    def _protection_prices(self, direction, stop, target, precision):
        if direction == Direction.LONG:
            # Long: a higher SL and lower TP are the conservative/tighter sides.
            return (
                self._round_price(stop, precision, "up"),
                self._round_price(target, precision, "down"),
            )
        return (
            self._round_price(stop, precision, "down"),
            self._round_price(target, precision, "up"),
        )

    @staticmethod
    def _data(response):
        if isinstance(response, dict):
            return response.get("data", response)
        return response

    @staticmethod
    def _rows(response):
        data = CoinWExecutor._data(response)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("rows", "data", "list"):
                if isinstance(data.get(key), list):
                    return data[key]
        return []

    async def _instrument_info(self, instrument):
        cached = self._instrument_cache.get(instrument)
        if cached is not None:
            return cached
        info = await self.orders.client.request(
            "GET", "/v1/perpum/instruments",
            {"name": instrument}, private=False
        )
        rows = self._rows(info)
        self._instrument_cache[instrument] = rows[0] if rows else {}
        return self._instrument_cache[instrument]

    async def _confirm_order(self, intent, order_id):
        instrument = self._instrument(intent.symbol)
        latest_order = None
        latest_position = None

        for _ in range(self.confirm_attempts):
            try:
                order_info = await self.orders.information(
                    [order_id], position_type="execute"
                )
                order_rows = self._rows(order_info)
                if order_rows:
                    latest_order = order_rows[0]

                pos_info = await self.positions.current(instrument)
                pos_rows = self._rows(pos_info)
                for row in pos_rows:
                    if str(row.get("instrument", "")).upper() == instrument and                        str(row.get("direction", "")).lower() == intent.direction.value.lower() and                        str(row.get("status", "")).lower() == "open":
                        latest_position = row
                        break

                order_status = str((latest_order or {}).get("orderStatus", "")).lower()
                if latest_position and order_status in {"finish", "part"}:
                    return latest_order, latest_position
                if order_status == "cancel":
                    return latest_order, None
            except Exception as exc:
                if self.audit:
                    self.audit.event(
                        "LIVE_CONFIRM_ERROR",
                        intent.decision_id,
                        order_id=str(order_id),
                        error=str(exc),
                    )
            await asyncio.sleep(self.confirm_delay)

        return latest_order, latest_position

    def _position_from_exchange(self, intent, row):
        entry = float(
            row.get("openPrice")
            or row.get("avgPrice")
            or row.get("orderPrice")
            or intent.entry_price
        )

        quantity = base_quantity(row, entry)

        position_id = str(
            row.get("id")
            or row.get("openId")
            or f"LIVE-{uuid4().hex[:16]}"
        )
        position = Position(
            position_id=position_id,
            decision_id=intent.decision_id,
            symbol=intent.symbol,
            direction=intent.direction,
            quantity=quantity,
            entry_price=entry,
            stop_price=float(intent.stop_price),
            target_price=float(intent.target_price),
            tp1_price=(
                float(intent.metadata["tp1_price"])
                if intent.metadata.get("tp1_price") is not None else None
            ),
            tp2_price=float(intent.metadata.get("tp2_price", intent.target_price)),
            remaining_quantity=quantity,
            opened_at=int(row.get("updatedDate") or row.get("createdDate") or 0),
            entry_fee=float(row.get("fee") or 0),
        )
        return apply_intent_management(position, intent)

    async def submit_intent(self, intent, quantity, leverage=10, position_model=0):
        if not self.enabled:
            return {
                "accepted": False,
                "filled": False,
                "blocked": True,
                "reason": "live_execution_disabled",
            }

        instrument = self._instrument(intent.symbol)
        info = await self._instrument_info(instrument)
        one_lot_size = float(info.get("oneLotSize") or 0)
        min_size = float(info.get("minSize") or 0)
        instrument_status = str(info.get("status") or "online").lower()
        max_leverage = float(info.get("maxLeverage") or leverage)
        price_precision = int(info.get("pricePrecision") or info.get("price_precision") or 8)
        normalized_stop, normalized_target = self._protection_prices(
            intent.direction, intent.stop_price, intent.target_price, price_precision
        )
        if instrument_status not in {"online", "1", "true", ""}:
            return {
                "accepted": False,
                "filled": False,
                "blocked": True,
                "reason": "instrument_not_tradable",
                "status": instrument_status,
            }
        if float(leverage) > max_leverage:
            return {
                "accepted": False,
                "filled": False,
                "blocked": True,
                "reason": "leverage_exceeds_instrument_max",
                "requested_leverage": leverage,
                "max_leverage": max_leverage,
            }

        # quantity is quote-currency notional for quantityUnit=0.
        base_estimate = quantity / max(intent.entry_price, 1e-12)
        min_base = one_lot_size * min_size if one_lot_size and min_size else 0.0
        if min_base and base_estimate < min_base:
            return {
                "accepted": False,
                "filled": False,
                "blocked": True,
                "reason": "quantity_below_exchange_minimum",
                "quantity": quantity,
                "minimum_base_quantity": min_base,
            }

        payload = {
            "instrument": instrument,
            "direction": "long" if intent.direction == Direction.LONG else "short",
            "leverage": int(leverage),
            "quantityUnit": 0,
            "quantity": str(quantity),
            "positionModel": int(position_model),
            "positionType": "execute",
            "thirdOrderId": intent.decision_id,
            "stopLossPrice": normalized_stop,
            "stopProfitPrice": normalized_target,
        }

        response = await self.orders.place(payload)
        data = self._data(response)
        order_id = data.get("value") if isinstance(data, dict) else data
        if order_id is None:
            return {
                "accepted": True,
                "filled": False,
                "exchange_response": response,
                "reason": "missing_order_id",
            }

        if self.audit:
            self.audit.event(
                "LIVE_ORDER_ACCEPTED",
                intent.decision_id,
                order_id=str(order_id),
                response=response,
            )

        order_row, position_row = await self._confirm_order(intent, order_id)
        if not position_row:
            # CoinW code=0 only means the request was received; it does not prove
            # execution. Keep this as PENDING rather than mis-labelling it as a
            # rejected trade. The next position reconciliation can promote a
            # delayed fill into POSITION_OPENED.
            return {
                "accepted": True,
                "filled": False,
                "order_id": str(order_id),
                "reason": "order_pending_confirmation",
                "exchange_response": response,
                "order_state": order_row,
                "next": "confirm_on_next_sync",
            }

        position = self._position_from_exchange(intent, position_row)
        position.leverage = int(leverage)
        position.stop_price = normalized_stop
        position.target_price = normalized_target
        position.tp2_price = normalized_target

        # Reassert exchange-native protection against an entry/fill race.
        try:
            await self.orders.set_tpsl(
                position.position_id,
                instrument,
                stop_loss=normalized_stop,
                take_profit=normalized_target,
            )
        except Exception as exc:
            if self.audit:
                self.audit.event(
                    "LIVE_PROTECTION_ERROR",
                    intent.decision_id,
                    position_id=position.position_id,
                    error=str(exc),
                )
            return {
                "accepted": True,
                "filled": True,
                "protected": False,
                "position_id": position.position_id,
                "position": position.__dict__,
                "order_id": str(order_id),
                "exchange_response": response,
                "error": "exchange_protection_failed",
            }

        return {
            "accepted": True,
            "filled": True,
            "protected": True,
            "position_id": position.position_id,
            "position": position.__dict__,
            "order_id": str(order_id),
            "fill_price": position.entry_price,
            "exchange_response": response,
            "order_state": order_row,
        }

    async def ensure_protection(self, position):
        instrument = self._instrument(position.symbol)
        info = await self._instrument_info(instrument)
        precision = int(info.get("pricePrecision") or info.get("price_precision") or 8)
        stop, target = self._protection_prices(
            position.direction, position.stop_price, position.target_price, precision
        )
        await self.orders.set_tpsl(
            position.position_id, instrument,
            stop_loss=stop, take_profit=target,
        )
        position.stop_price = stop
        position.target_price = target
        if position.tp2_price is not None:
            position.tp2_price = target
        return position

    async def pending_order_status(self, order_id):
        """Return CoinW's current status for an accepted-but-unconfirmed order."""
        if not order_id:
            return {"status": "unknown", "order": None}
        try:
            response = await self.orders.information([order_id], position_type="execute")
            rows = self._rows(response)
            row = rows[0] if rows else None
            status = str((row or {}).get("orderStatus", "")).lower() or "unknown"
            return {"status": status, "order": row}
        except Exception as exc:
            return {"status": "unknown", "order": None, "error": f"{type(exc).__name__}: {exc}"}

    async def confirm(self, instrument, order_id=None, position_ids=None):
        result = {"orders": None, "positions": None}
        if order_id:
            result["orders"] = await self.orders.information(
                [order_id], position_type="execute"
            )
        result["positions"] = await self.positions.current(
            self._instrument(instrument), position_ids
        )
        return result

    async def current_position_rows(self, instrument):
        response = await self.positions.current(self._instrument(instrument))
        return self._rows(response)

    async def sync_positions(self, instruments):
        if isinstance(instruments, str):
            instruments = [instruments]
        snapshots = []
        for instrument in instruments:
            snapshots.append(
                await self.positions.current(self._instrument(instrument))
            )
        return snapshots

    async def equity(self):
        if not self.account:
            return 0.0
        response = await self.account.assets("usdt")
        data = self._data(response)
        if isinstance(data, dict):
            return float(
                data.get("availableUsdt")
                or data.get("availableMargin")
                or 0.0
            )
        return 0.0

    async def settlements(self, instrument, position_ids):
        """Only use explicit, complete closing events. Unknown means keep reconciling."""
        import math
        rows = self._rows(await self.positions.history(self._instrument(instrument)))
        result = {}
        for pid in position_ids:
            unique = {}
            for row in rows:
                if (str(row.get('openId')) == str(pid)
                        and str(row.get('status', '')).lower() == 'close'
                        and str(row.get('orderStatus', '')).lower() == 'finish'
                        and row.get('netProfit') is not None):
                    unique[str(row.get('orderId') or '')] = row
            closes = list(unique.values())
            if not closes:
                continue
            try:
                weights = [float(r['tradePiece']) for r in closes]
                total = max(float(r['totalPiece']) for r in closes)
                prices = [float(r.get('avgClosePrice') or r.get('closePrice')) for r in closes]
                pnls = [float(r['netProfit']) for r in closes]
                if (not all(math.isfinite(v) for v in [*weights, total, *prices, *pnls])
                        or min(weights) <= 0 or min(prices) <= 0 or total <= 0
                        or sum(weights) < total):
                    continue
                result[str(pid)] = {
                    'net_pnl': sum(pnls),
                    'exit_price': sum(p * w for p, w in zip(prices, weights)) / sum(weights),
                    'closed_at': max(int(r['tradeStartDate']) for r in closes),
                }
            except (KeyError, ValueError, TypeError):
                continue
        return result
