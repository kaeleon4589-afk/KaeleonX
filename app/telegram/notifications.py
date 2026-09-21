from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.telegram.bot import TelegramBotService

logger = logging.getLogger("kaeleon.telegram.trades")


def _fmt_money(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _direction(position: Any) -> str:
    value = getattr(position, "direction", None)
    return str(getattr(value, "value", value) or "—").upper()


class TelegramTradeNotifier:
    """Best-effort user trade notifications.

    Notifications never control order execution. Failures are logged and isolated so
    a Telegram outage cannot block the trading engine.
    """

    def __init__(self, settings, db, auth):
        self.db = db
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
            return
        chat_id = self._chat_id(user_id)
        if not chat_id:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("No running loop for Telegram trade notification user=%s", user_id)
            return
        loop.create_task(self._send(chat_id, text, user_id))

    async def _send(self, chat_id: str, text: str, user_id: str) -> None:
        try:
            await self.bot.send_message(chat_id, text)
        except Exception:
            logger.exception("Unable to send Telegram trade notification user=%s", user_id)

    def position_opened(self, user_id: str, mode: str, position: Any) -> None:
        text = (
            "🟢 KAELEON — Operación abierta\n\n"
            f"Modo: {mode.upper()}\n"
            f"Par: {getattr(position, 'symbol', '—')}\n"
            f"Dirección: {_direction(position)}\n"
            f"Entrada: {_fmt_money(getattr(position, 'entry_price', None))}\n"
            f"Cantidad: {_fmt_money(getattr(position, 'quantity', None))}\n"
            f"Stop Loss: {_fmt_money(getattr(position, 'stop_price', None))}\n"
            f"Take Profit: {_fmt_money(getattr(position, 'target_price', None))}"
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
            f"Dirección: {_direction(position)}\n"
            f"Resultado: {result}\n"
            f"Entrada: {_fmt_money(getattr(position, 'entry_price', None))}\n"
            f"Salida: {_fmt_money(getattr(position, 'exit_price', None))}\n"
            f"PnL neto: {'+' if net > 0 else ''}{_fmt_money(net)} USDT\n"
            f"Motivo: {getattr(position, 'exit_reason', None) or 'EXCHANGE'}"
        )
        self._schedule(user_id, text)
