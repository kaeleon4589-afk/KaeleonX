from fastapi import APIRouter, Header, HTTPException, Request

from app.auth.service import AuthService
from app.config.settings import get_settings
from app.storage.database import Database
from app.telegram.bot import TelegramBotService, TelegramBotError

router = APIRouter(prefix="/telegram", tags=["telegram"])
_db = None
_auth = None
_bot = None


def bot_service() -> TelegramBotService:
    global _db, _auth, _bot
    if _bot is None:
        settings = get_settings()
        if not settings.telegram_enabled:
            raise HTTPException(503, "telegram_verification_disabled")
        if not settings.telegram_registration_configured:
            raise HTTPException(503, "telegram_bot_not_configured")
        _db = Database(settings.mongodb_uri, settings.mongodb_database)
        _auth = AuthService(_db)
        _bot = TelegramBotService(
            _auth,
            _db,
            token=settings.telegram_bot_token,
            username=settings.telegram_bot_username,
            webhook_url=settings.telegram_webhook_url,
            webhook_secret=settings.telegram_webhook_secret,
            timeout_seconds=settings.telegram_api_timeout_seconds,
        )
    return _bot


@router.post("/webhook")
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    bot = bot_service()
    if bot.webhook_secret and x_telegram_bot_api_secret_token != bot.webhook_secret:
        raise HTTPException(401, "invalid_telegram_webhook_secret")
    try:
        update = await request.json()
        await bot.handle_update(update)
    except TelegramBotError:
        raise HTTPException(502, "telegram_api_error")
    return {"ok": True}
