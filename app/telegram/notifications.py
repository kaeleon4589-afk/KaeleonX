from __future__ import annotations

import asyncio
import logging
import hashlib
import time
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
        self._delivery_lock = asyncio.Lock()
        self._tasks = set()
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
        task = asyncio.create_task(self._enqueue(user_id, text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _enqueue(self, user_id, text):
        event_id = hashlib.sha256(f'{user_id}|{text}'.encode()).hexdigest()
        try:
            await asyncio.to_thread(self.db.set_once, 'notification_outbox', {'event_id': event_id}, {
                'user_id': user_id, 'text': text, 'status': 'pending', 'attempts': 0,
                'next_attempt_at': 0.0,
            })
            await self.deliver_pending()
        except Exception as exc:
            if self.audit:
                self.audit.event('TELEGRAM_NOTIFY_FAILED', user_id, level='ERROR',
                                 user_id=user_id, error=f'outbox:{type(exc).__name__}')

    async def deliver_pending(self):
        if not self.bot:
            return
        async with self._delivery_lock:
            rows = await asyncio.to_thread(self.db.find_many, 'notification_outbox',
                                           {'status': 'pending'}, limit=50,
                                           sort_field='next_attempt_at', descending=False)
            for row in rows:
                if row.get('next_attempt_at', 0) > time.time():
                    continue
                key = {'event_id': row['event_id']}
                try:
                    chat = await asyncio.to_thread(self._chat_id, row['user_id'])
                    if not chat:
                        raise RuntimeError('telegram_chat_id_missing')
                    await self.bot.send_message(chat, row['text'])
                    await asyncio.to_thread(self.db.upsert, 'notification_outbox', key,
                                            {'status': 'sent', 'sent_at': time.time()})
                    if self.audit:
                        self.audit.event('TELEGRAM_NOTIFY_SENT', row['user_id'], user_id=row['user_id'])
                except Exception as exc:
                    attempts = int(row.get('attempts', 0)) + 1
                    await asyncio.to_thread(self.db.upsert, 'notification_outbox', key, {
                        'attempts': attempts, 'next_attempt_at': time.time() + min(300, 2 ** min(attempts, 8)),
                        'last_error': type(exc).__name__,
                    })
                    if self.audit:
                        self.audit.event('TELEGRAM_NOTIFY_FAILED', row['user_id'], level='ERROR',
                                         user_id=row['user_id'], error=type(exc).__name__, attempt=attempts)

    async def run(self):
        while True:
            try:
                await self.deliver_pending()
            except Exception as exc:
                if self.audit:
                    self.audit.event('TELEGRAM_NOTIFY_FAILED', 'system', level='ERROR', error=type(exc).__name__)
            await asyncio.sleep(5)

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
            f"Modo: {mode.upper()} · {getattr(position, 'leverage', 10)}x\n"
            f"ID: {getattr(position, 'position_id', '—')}\n"
            f"Par: {getattr(position, 'symbol', '—')}\n"
            f"Estrategia: {_strategy(position)}\n"
            f"Dirección: {_direction(position)}\n"
            f"Entrada: {_fmt_number(getattr(position, 'entry_price', None), price=True)}\n"
            f"Cantidad: {_fmt_number(getattr(position, 'quantity', None))}\n"
            f"Stop Loss: {_fmt_number(getattr(position, 'stop_price', None), price=True)}\n"
            f"Take Profit: {_fmt_number(getattr(position, 'target_price', None), price=True)}\n"
            f"RR ejecución: {_fmt_number(getattr(position, 'execution_rr', None))}\n"
            f"Protección: {'confirmada' if getattr(position, 'protected', True) else 'PENDIENTE — reintentando SL/TP'}"
        )
        self._schedule(user_id, text)

    def position_closed(self, user_id: str, mode: str, position: Any) -> None:
        realized = float(getattr(position, "realized_pnl", 0.0) or 0.0)
        fees = float(getattr(position, "entry_fee", 0.0) or 0.0) + float(getattr(position, "exit_fee", 0.0) or 0.0)
        funding = float(getattr(position, "funding_pnl", 0.0) or 0.0)
        net = getattr(position, "net_pnl", None)
        net = realized - fees + funding if net is None else float(net)
        result = "WIN ✅" if net > 0 else "LOSS ❌" if net < 0 else "BREAKEVEN ⚪"
        text = (
            "🏁 KAELEON — Operación cerrada\n\n"
            f"Modo: {mode.upper()} · {getattr(position, 'leverage', 10)}x\n"
            f"ID: {getattr(position, 'position_id', '—')}\n"
            f"Par: {getattr(position, 'symbol', '—')}\n"
            f"Estrategia: {_strategy(position)}\n"
            f"Dirección: {_direction(position)}\n"
            f"Resultado: {result}\n"
            f"Entrada: {_fmt_number(getattr(position, 'entry_price', None), price=True)}\n"
            f"Salida: {_fmt_number(getattr(position, 'exit_price', None), price=True)}\n"
            + (f"Stop Loss: {_fmt_number(getattr(position, 'stop_price', None), price=True)}\n"
               f"Diferencia frente al SL: {float(getattr(position, 'stop_gap_bps', 0) or 0)/100:.4f}%\n"
               if getattr(position, 'exit_reason', None) == 'SL' and getattr(position, 'stop_gap_bps', None) is not None else "")
            + f"PnL neto: {'+' if net > 0 else ''}{_fmt_number(net)} USDT\n"
            f"Motivo: {getattr(position, 'exit_reason', None) or 'EXCHANGE'}"
        )
        self._schedule(user_id, text)
