from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Header, Request
from pydantic import BaseModel, Field

from app.auth.service import AuthService, token_hash
from app.config.settings import get_settings
from app.storage.database import Database
from app.telegram.bot import TelegramBotError, TelegramBotService

router = APIRouter(prefix="/auth", tags=["auth"])
_db = None
_service = None
logger = logging.getLogger("kaeleon.auth")


def service():
    global _db, _service
    if _service is None:
        s = get_settings()
        if s.telegram_enabled and not s.telegram_registration_configured:
            raise HTTPException(503, "telegram_verification_not_configured")
        _db = Database(s.mongodb_uri, s.mongodb_database)
        _service = AuthService(_db)
    return _service


def telegram_bot() -> TelegramBotService | None:
    settings = get_settings()
    if not settings.telegram_enabled or not settings.telegram_registration_configured:
        return None
    auth = service()
    return TelegramBotService(
        auth,
        _db,
        token=settings.telegram_bot_token,
        username=settings.telegram_bot_username,
        webhook_url=settings.telegram_webhook_url,
        webhook_secret=settings.telegram_webhook_secret,
        timeout_seconds=settings.telegram_api_timeout_seconds,
    )


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    if forwarded:
        return forwarded[:96]
    return (request.client.host if request.client else "")[:96]


async def send_security_message(chat_id: str | int | None, text: str) -> bool:
    if not chat_id:
        return False
    bot = telegram_bot()
    if bot is None:
        return False
    try:
        await bot.send_message(chat_id, text)
        return True
    except TelegramBotError:
        logger.exception("Unable to send Telegram security notification")
        return False


class RegisterRequest(BaseModel):
    phone: str = Field(min_length=7)
    password: str = Field(min_length=8)
    country_code: str = ""
    referral_code: str = ""


class VerifyRequest(BaseModel):
    challenge: str = Field(min_length=20)


class LoginRequest(BaseModel):
    phone: str = Field(min_length=7)
    password: str = Field(min_length=8)
    country_code: str = ""


class TutorialRequest(BaseModel):
    completed: bool = True


class RecoveryCodeEnrollRequest(BaseModel):
    recovery_code: str = Field(min_length=20, max_length=40)


class RecoveryCodeRegenerateRequest(BaseModel):
    current_password: str = Field(min_length=8)


class TelegramPasswordRecoveryRequest(BaseModel):
    phone: str = Field(min_length=7)
    country_code: str = ""


class TelegramPasswordRecoveryConfirmRequest(BaseModel):
    challenge: str = Field(min_length=20)
    otp: str = Field(pattern=r"^\d{6}$")
    new_password: str = Field(min_length=8)


class RecoveryCodePasswordResetRequest(BaseModel):
    phone: str = Field(min_length=7)
    country_code: str = ""
    recovery_code: str = Field(min_length=20, max_length=40)
    new_password: str = Field(min_length=8)


def bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "authentication_required")
    return authorization.split(" ", 1)[1].strip()


@router.post("/register")
def register(req: RegisterRequest):
    try:
        user = service().create_registration(req.phone, req.password, req.country_code, req.referral_code)
        settings = get_settings()
        telegram_url = ""
        if settings.telegram_enabled:
            if not settings.telegram_registration_configured:
                raise HTTPException(503, "telegram_verification_not_configured")
            telegram_url = f"https://t.me/{settings.telegram_bot_username.lstrip('@')}?start={user['challenge']}"
        return {
            **user,
            "telegram_verification_url": telegram_url,
            "telegram_required": bool(settings.telegram_enabled),
        }
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/verify")
def verify(req: VerifyRequest):
    if not service().verify_registration(req.challenge):
        raise HTTPException(400, "telegram_verification_required_or_invalid")
    # Recovery code enrollment happens immediately after the first authenticated
    # login, before the dashboard. The raw recovery code is generated in the
    # browser and only its scrypt hash is persisted by /recovery-code/enroll.
    return {"verified": True, "recovery_code_required": True}


@router.post("/login")
def login(req: LoginRequest):
    try:
        token = service().login(req.phone, req.password, req.country_code)
        return {"access_token": token, "token_type": "bearer"}
    except ValueError:
        raise HTTPException(401, "invalid_credentials")


@router.get("/me")
def me(authorization: str | None = Header(default=None)):
    token = bearer_token(authorization)
    current = service().get_current_user(token)
    if not current:
        raise HTTPException(401, "invalid_or_expired_session")
    return service().public_user(current)


@router.post("/recovery-code/enroll")
def enroll_recovery_code(req: RecoveryCodeEnrollRequest, authorization: str | None = Header(default=None)):
    token = bearer_token(authorization)
    current = service().get_current_user(token)
    if not current:
        raise HTTPException(401, "invalid_or_expired_session")
    try:
        result = service().enroll_recovery_code(current["user_id"], req.recovery_code)
        return {"configured": bool(result["configured"])}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/recovery-code/regenerate")
async def regenerate_recovery_code(req: RecoveryCodeRegenerateRequest, authorization: str | None = Header(default=None)):
    token = bearer_token(authorization)
    current = service().get_current_user(token)
    if not current:
        raise HTTPException(401, "invalid_or_expired_session")
    try:
        result = service().regenerate_recovery_code(current["user_id"], req.current_password)
    except ValueError as exc:
        detail = str(exc)
        if detail not in {"current_password_invalid", "invalid_user"}:
            detail = "current_password_invalid"
        raise HTTPException(400, detail)
    await send_security_message(
        result.get("telegram_chat_id"),
        "🔐 KAELEON — Tu código de recuperación fue regenerado. El código anterior ya no es válido. "
        "Si no realizaste este cambio, restablece tu contraseña inmediatamente.",
    )
    return {"recovery_code": result["recovery_code"]}


@router.post("/password-recovery/telegram/request")
async def request_telegram_password_recovery(req: TelegramPasswordRecoveryRequest, request: Request):
    try:
        result = service().begin_telegram_password_recovery(
            req.phone,
            req.country_code,
            requester_ip=client_ip(request),
        )
    except ValueError:
        # Invalid phone syntax can be reported without revealing whether an
        # account exists.
        raise HTTPException(400, "invalid_phone")

    if result.get("deliver"):
        delivered = await send_security_message(
            result.get("telegram_chat_id"),
            "🔐 KAELEON — Recuperación de contraseña\n\n"
            f"Tu código de verificación es: {result['otp']}\n\n"
            "Caduca en 10 minutos y solo puede usarse una vez. "
            "Si no solicitaste este cambio, ignora este mensaje.",
        )
        if not delivered:
            # Keep the public response neutral. Invalidate the undelivered
            # challenge so it can never be used if Telegram failed.
            service().db.upsert(
                "password_recovery_challenges",
                {"challenge_hash": token_hash(result["challenge"])},
                {"used": True, "invalidated_reason": "telegram_delivery_failed"},
            )

    return {
        "accepted": True,
        "challenge": result["challenge"],
        "expires_in_seconds": 600,
        "message": "Si la cuenta puede recuperarse por Telegram, recibirás un código de verificación.",
    }


@router.post("/password-recovery/telegram/confirm")
async def confirm_telegram_password_recovery(req: TelegramPasswordRecoveryConfirmRequest, request: Request):
    try:
        result = service().complete_telegram_password_recovery(
            req.challenge,
            req.otp,
            req.new_password,
            requester_ip=client_ip(request),
        )
    except ValueError as exc:
        detail = str(exc)
        if detail not in {"invalid_or_expired_recovery_challenge", "invalid_recovery_code", "password_too_short"}:
            detail = "invalid_or_expired_recovery_challenge"
        raise HTTPException(400, detail)

    await send_security_message(
        result.get("telegram_chat_id"),
        "✅ KAELEON — Tu contraseña fue restablecida correctamente. "
        "Todas las sesiones anteriores fueron cerradas. Si no fuiste tú, contacta a soporte inmediatamente.",
    )
    return {"reset": True, "recovery_code": result["recovery_code"]}


@router.post("/password-recovery/recovery-code/reset")
async def reset_password_with_recovery_code(req: RecoveryCodePasswordResetRequest, request: Request):
    try:
        result = service().reset_password_with_recovery_code(
            req.phone,
            req.recovery_code,
            req.new_password,
            req.country_code,
            requester_ip=client_ip(request),
        )
    except ValueError as exc:
        detail = str(exc)
        if detail not in {"invalid_recovery_credentials", "recovery_temporarily_locked", "password_too_short", "invalid_phone"}:
            detail = "invalid_recovery_credentials"
        raise HTTPException(400, detail)

    await send_security_message(
        result.get("telegram_chat_id"),
        "✅ KAELEON — Tu contraseña fue restablecida usando tu código de recuperación. "
        "Todas las sesiones anteriores fueron cerradas y tu código anterior quedó invalidado.",
    )
    return {"reset": True, "recovery_code": result["recovery_code"]}


@router.post("/tutorial")
def tutorial(req: TutorialRequest, authorization: str | None = Header(default=None)):
    token = bearer_token(authorization)
    current = service().get_current_user(token)
    if not current:
        raise HTTPException(401, "invalid_or_expired_session")
    service().set_tutorial_completed(current["user_id"], req.completed)
    return {"tutorial_completed": bool(req.completed)}


@router.post("/logout")
def logout(authorization: str | None = Header(default=None)):
    token = bearer_token(authorization)
    if not service().revoke_session(token):
        raise HTTPException(401, "invalid_or_expired_session")
    return {"logged_out": True}
