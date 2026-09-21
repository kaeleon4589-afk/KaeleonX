from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth_api import router as auth_router
from app.api.admin_api import router as admin_router
from app.api.billing_api import router as billing_router
from app.api.user_api import router as user_router
from app.api.telegram_api import router as telegram_router
from app.auth.service import AuthService
from app.config.settings import get_settings
from app.storage.database import Database
from app.telegram.bot import TelegramBotService, TelegramBotError

logger = logging.getLogger("kaeleon.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.environment == "production":
        if not settings.mongodb_uri.strip():
            raise RuntimeError("mongodb_uri_required_in_production")
        if not settings.credential_encryption_key.strip():
            raise RuntimeError("credential_encryption_key_required_in_production")
    if settings.telegram_enabled:
        if not settings.telegram_registration_configured:
            logger.error("Telegram registration verification is enabled but the required Telegram configuration is incomplete")
        elif settings.telegram_auto_set_webhook and settings.telegram_webhook_url:
            try:
                db = Database(settings.mongodb_uri, settings.mongodb_database)
                bot = TelegramBotService(
                    AuthService(db),
                    db,
                    settings.telegram_bot_token,
                    settings.telegram_bot_username,
                    settings.telegram_webhook_url,
                    settings.telegram_webhook_secret,
                    settings.telegram_api_timeout_seconds,
                )
                me = await bot.get_me()
                if me.get("username") and me["username"].lower() != settings.telegram_bot_username.lstrip("@").lower():
                    logger.error("Configured Telegram username does not match Bot API username: configured=%s actual=%s", settings.telegram_bot_username, me.get("username"))
                await bot.set_webhook()
                logger.info("Telegram webhook configured for @%s", me.get("username") or settings.telegram_bot_username)
            except (TelegramBotError, Exception):
                logger.exception("Unable to configure Telegram webhook during startup")
    yield


app = FastAPI(title="KAELEON API", version="0.12.0", lifespan=lifespan)
_cors_origins = [origin.strip() for origin in get_settings().cors_allowed_origins.split(",") if origin.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
app.include_router(auth_router)
app.include_router(telegram_router)
app.include_router(admin_router)
app.include_router(billing_router)
app.include_router(user_router)


@app.get("/health")
def health():
    settings = get_settings()
    telegram_configured = bool(settings.telegram_enabled and settings.telegram_registration_configured)
    return {
        "status": "ok",
        "service": "kaeleon",
        "version": "0.12.0",
        "telegram_verification": "configured" if telegram_configured else "disabled_or_unconfigured",
        "credential_vault": "configured" if settings.credential_encryption_key else "unconfigured",
    }
