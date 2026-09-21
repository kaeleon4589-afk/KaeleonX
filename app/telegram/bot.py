from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.auth.service import AuthService

logger = logging.getLogger("kaeleon.telegram")


class TelegramBotError(RuntimeError):
    pass


class TelegramBotService:
    """Small, dependency-free Telegram Bot API integration for registration verification."""

    def __init__(
        self,
        auth: AuthService,
        db,
        token: str,
        username: str,
        webhook_url: str = "",
        webhook_secret: str = "",
        timeout_seconds: float = 10.0,
    ):
        self.auth = auth
        self.db = db
        self.token = token.strip()
        self.username = username.strip().lstrip("@").strip()
        self.webhook_url = webhook_url.strip()
        self.webhook_secret = webhook_secret.strip()
        self.timeout_seconds = timeout_seconds
        self.base_url = f"https://api.telegram.org/bot{self.token}" if self.token else ""

    @property
    def configured(self) -> bool:
        return bool(self.token and self.username)

    @property
    def verification_url_template(self) -> str:
        return f"https://t.me/{self.username}?start={{challenge}}" if self.username else ""

    async def _call(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.token:
            raise TelegramBotError("telegram_bot_token_missing")
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(f"{self.base_url}/{method}", json=payload or {})
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TelegramBotError(f"telegram_api_error:{method}") from exc
        if not data.get("ok"):
            raise TelegramBotError(f"telegram_api_rejected:{method}:{data.get('description', 'unknown')}")
        return data

    async def get_me(self) -> dict[str, Any]:
        result = await self._call("getMe")
        return result.get("result", {})

    async def set_webhook(self, drop_pending_updates: bool = False) -> dict[str, Any]:
        if not self.webhook_url:
            raise TelegramBotError("telegram_webhook_url_missing")
        if not self.webhook_url.startswith("https://"):
            raise TelegramBotError("telegram_webhook_url_must_use_https")
        payload: dict[str, Any] = {
            "url": self.webhook_url,
            "allowed_updates": ["message"],
            "drop_pending_updates": drop_pending_updates,
        }
        if self.webhook_secret:
            payload["secret_token"] = self.webhook_secret
        return await self._call("setWebhook", payload)

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self._call("sendMessage", payload)

    async def clear_keyboard(self, chat_id: int | str, text: str) -> dict[str, Any]:
        return await self.send_message(
            chat_id,
            text,
            {"remove_keyboard": True},
        )

    def _save_chat_state(self, chat_id: str, challenge_hash: str, telegram_user_id: str, expires_at: datetime) -> None:
        self.db.upsert(
            "telegram_bot_sessions",
            {"chat_id": chat_id},
            {
                "chat_id": chat_id,
                "challenge_hash": challenge_hash,
                "telegram_user_id": str(telegram_user_id),
                "expires_at": expires_at,
                "active": True,
            },
        )

    def _chat_state(self, chat_id: str) -> dict[str, Any] | None:
        state = self.db.find_one("telegram_bot_sessions", {"chat_id": chat_id})
        if not state or not state.get("active", True):
            return None
        expires_at = state.get("expires_at")
        if expires_at and expires_at < datetime.now(timezone.utc):
            self.db.upsert("telegram_bot_sessions", {"chat_id": chat_id}, {"active": False})
            return None
        return state

    async def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        from_user = message.get("from") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        telegram_user_id = from_user.get("id")
        if chat_id is None or telegram_user_id is None:
            return

        contact = message.get("contact")
        if contact:
            await self._handle_contact(chat_id, str(telegram_user_id), contact)
            return

        text = str(message.get("text") or "").strip()
        if text.startswith("/start"):
            challenge = text[len("/start"):].strip().split(" ", 1)[0]
            await self._handle_start(chat_id, str(telegram_user_id), challenge)
            return

        state = self._chat_state(str(chat_id))
        if state:
            await self.send_message(
                chat_id,
                "Para completar el registro, pulsa el botón «Compartir mi número» y comparte tu propio contacto.",
                self._contact_keyboard(),
            )
        else:
            await self.send_message(
                chat_id,
                "No hay una verificación de registro activa. Vuelve a KAELEON y abre el enlace de verificación desde el registro.",
            )

    async def _handle_start(self, chat_id: int | str, telegram_user_id: str, challenge: str) -> None:
        if not challenge:
            await self.send_message(
                chat_id,
                "Este bot se usa para verificar el registro de KAELEON. Primero inicia el registro en KAELEON y abre el enlace que te proporciona.",
            )
            return

        info = self.auth.registration_challenge_info(challenge)
        if not info:
            await self.send_message(chat_id, "El enlace de verificación no es válido o ha expirado. Genera un nuevo registro en KAELEON.")
            return

        self._save_chat_state(
            str(chat_id),
            info["challenge_hash"],
            telegram_user_id,
            info["expires_at"],
        )
        await self.send_message(
            chat_id,
            "Vamos a verificar tu número de teléfono. Pulsa el botón de abajo y comparte tu propio número. Debe coincidir exactamente con el número que registraste en KAELEON.",
            self._contact_keyboard(),
        )

    async def _handle_contact(self, chat_id: int | str, telegram_user_id: str, contact: dict[str, Any]) -> None:
        state = self._chat_state(str(chat_id))
        if not state:
            await self.clear_keyboard(chat_id, "No hay una verificación de registro activa. Abre primero el enlace de verificación desde KAELEON.")
            return

        contact_user_id = contact.get("user_id")
        if contact_user_id is None or str(contact_user_id) != str(telegram_user_id):
            await self.clear_keyboard(
                chat_id,
                "Por seguridad, debes usar el botón para compartir tu propio número. No se aceptan contactos reenviados o de otra persona.",
            )
            return

        phone = str(contact.get("phone_number") or "").strip()
        if not phone:
            await self.send_message(chat_id, "Telegram no envió un número válido. Vuelve a pulsar «Compartir mi número»." )
            return

        verified = self.auth.mark_telegram_contact_by_hash(
            state["challenge_hash"],
            telegram_user_id,
            phone,
            str(chat_id),
        )
        if not verified:
            await self.clear_keyboard(
                chat_id,
                "El número de Telegram no coincide con el número del registro o el enlace ya expiró. Genera un registro nuevo en KAELEON.",
            )
            self.db.upsert("telegram_bot_sessions", {"chat_id": str(chat_id)}, {"active": False})
            return

        self.db.upsert(
            "telegram_bot_sessions",
            {"chat_id": str(chat_id)},
            {"active": False, "verified_at": datetime.now(timezone.utc)},
        )
        await self.clear_keyboard(
            chat_id,
            "✅ Número verificado correctamente. Vuelve a KAELEON y pulsa «VERIFICAR CUENTA» para activar tu cuenta.",
        )

    @staticmethod
    def _contact_keyboard() -> dict[str, Any]:
        return {
            "keyboard": [[{"text": "📱 Compartir mi número", "request_contact": True}]],
            "resize_keyboard": True,
            "one_time_keyboard": True,
        }
