from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, Field
from app.auth.service import AuthService
from app.config.settings import get_settings
from app.storage.database import Database

router = APIRouter(prefix="/auth", tags=["auth"])
_db = None
_service = None


def service():
    global _db, _service
    if _service is None:
        s = get_settings()
        if s.telegram_enabled and not s.telegram_registration_configured:
            raise HTTPException(503, "telegram_verification_not_configured")
        _db = Database(s.mongodb_uri, s.mongodb_database)
        _service = AuthService(_db)
    return _service


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
    return {"verified": True}


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
