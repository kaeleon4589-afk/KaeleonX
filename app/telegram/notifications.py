from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.telegram.bot import TelegramBotService

logger = logging.getLogger("kaeleon.telegram.trades")


def _fmt_number(value: Any, *, price: bool = False) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if price:
        absolute = abs(number)
        decimals = 2 if absolute >= 100 else 4 if absolute >= 1 else 6
        return f"{number:,.{decimals}f}"
    return f"{number:,.6f}".rstrip("0").rstrip(".")


def _strategy(position: Any) -> str:
    value = getattr(position, "strategy", None)
    return str(value or "—").upper()


def _direction(position: Any) -> str:
    value = getattr(position, "direction", None)
    return str(getattr(value, "value", value) or "—").upper()


class TelegramTradeNotifier:
    """Best-effort user trade notifications.

    Notifications never control order execution. Failures are logged and isolated so
    a Telegram outage cannot block the trading engine.
    """

    def __init__(self, settings, db, auth, audit=None):
        self.db = db
        self.audit = audit
        self.enabled = bool(settings.telegram_enabled and settings.telegram_bot_token)
        self.bot = TelegramBotService(
            auth,
            db,
            settings.telegram_bot_token,
            settings.telegram_bot_username,
            settings.telegram_webhook_url,
            settings.telegram_webhook_secret,
            settings.telegram_api_timeout_seconds,
        ) if self.enabled else None

    def _chat_id(self, user_id: str) -> str | None:
        user = self.db.find_one("users", {"user_id": user_id}) or {}
        value = user.get("telegram_chat_id") or user.get("telegram_user_id")
        return str(value) if value else None

    def _schedule(self, user_id: str, text: str) -> None:
        if not self.bot:
            if self.audit:
                self.audit.event('TELEGRAM_NOTIFY_SKIPPED', user_id, level='DEBUG', user_id=user_id, reason='bot_disabled')
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("No running loop for Telegram trade notification user=%s", user_id)
            return
        if self.audit:
            self.audit.event('TELEGRAM_NOTIFY_QUEUED', user_id, level='DEBUG', user_id=user_id, message_type='trade')
        # Do not perform a synchronous Mongo lookup in the trading coroutine.
        loop.create_task(self._deliver_for_user(user_id, text))

    async def _deliver_for_user(self, user_id: str, text: str) -> None:
        try:
            chat_id = await asyncio.to_thread(self._chat_id, user_id)
        except Exception as exc:
            if self.audit:
                self.audit.event('TELEGRAM_NOTIFY_FAILED', user_id, level='ERROR', user_id=user_id, status='failed', error=f'chat_lookup:{exc}')
            return
        if not chat_id:
            if self.audit:
                self.audit.event('TELEGRAM_NOTIFY_SKIPPED', user_id, level='DEBUG', user_id=user_id, reason='chat_id_missing')
            return
        await self._send(chat_id, text, user_id)

    async def _send(self, chat_id: str, text: str, user_id: str) -> None:
        try:
            await self.bot.send_message(chat_id, text)
            if self.audit:self.audit.event('TELEGRAM_NOTIFY_SENT', user_id, user_id=user_id, status='sent')
        except Exception as exc:
            if self.audit:self.audit.event('TELEGRAM_NOTIFY_FAILED', user_id, level='ERROR', user_id=user_id, status='failed', error=str(exc))
            logger.exception("Unable to send Telegram trade notification user=%s", user_id)

    def position_opened(self, user_id: str, mode: str, position: Any) -> None:
        text = (
            "🟢 KAELEON — Operación abierta\n\n"
            f"Modo: {mode.upper()}\n"
            f"Par: {getattr(position, 'symbol', '—')}\n"
            f"Estrategia: {_strategy(position)}\n"
            f"Dirección: {_direction(position)}\n"
            f"Entrada: {_fmt_number(getattr(position, 'entry_price', None), price=True)}\n"
            f"Cantidad: {_fmt_number(getattr(position, 'quantity', None))}\n"
            f"Stop Loss: {_fmt_number(getattr(position, 'stop_price', None), price=True)}\n"
            f"Take Profit: {_fmt_number(getattr(position, 'target_price', None), price=True)}\n"
            f"RR ejecución: {_fmt_number(getattr(position, 'execution_rr', None))}"
        )
        self._schedule(user_id, text)

    def position_closed(self, user_id: str, mode: str, position: Any) -> None:
        realized = float(getattr(position, "realized_pnl", 0.0) or 0.0)
        fees = float(getattr(position, "entry_fee", 0.0) or 0.0) + float(getattr(position, "exit_fee", 0.0) or 0.0)
        funding = float(getattr(position, "funding_pnl", 0.0) or 0.0)
        net = realized - fees + funding
        result = "WIN ✅" if net > 0 else "LOSS ❌" if net < 0 else "BREAKEVEN ⚪"
        text = (
            "🏁 KAELEON — Operación cerrada\n\n"
            f"Modo: {mode.upper()}\n"
            f"Par: {getattr(position, 'symbol', '—')}\n"
            f"Estrategia: {_strategy(position)}\n"
            f"Dirección: {_direction(position)}\n"
            f"Resultado: {result}\n"
            f"Entrada: {_fmt_number(getattr(position, 'entry_price', None), price=True)}\n"
            f"Salida: {_fmt_number(getattr(position, 'exit_price', None), price=True)}\n"
            f"PnL neto: {'+' if net > 0 else ''}{_fmt_number(net)} USDT\n"
            f"Motivo: {getattr(position, 'exit_reason', None) or 'EXCHANGE'}"
        )
        self._schedule(user_id, text)
