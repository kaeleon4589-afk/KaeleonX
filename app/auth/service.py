import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.referrals import new_referral_code, attach_referral

E164_MAX = 15
SCRYPT_MAXMEM = 64 * 1024 * 1024


def normalize_phone(phone: str, country_code: str = "") -> str:
    raw = (phone or "").strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if not raw:
        raise ValueError("phone_required")
    if not raw.startswith("+"):
        cc = (country_code or "").strip()
        if not cc.startswith("+"):
            cc = "+" + cc if cc else ""
        raw = cc + raw.lstrip("+")
    digits = raw[1:]
    if not digits.isdigit() or not (7 <= len(digits) <= E164_MAX):
        raise ValueError("invalid_phone")
    return "+" + digits


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("password_too_short")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=SCRYPT_MAXMEM)
    return f"scrypt$32768$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=SCRYPT_MAXMEM,
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def expiry(hours: int = 24) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def as_utc(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class AuthService:
    def __init__(self, db, session_hours: int = 24):
        self.db = db
        self.session_hours = session_hours

    def create_registration(self, phone: str, password: str, country_code: str = "", referral_code: str = "") -> dict:
        normalized = normalize_phone(phone, country_code)
        existing = self.db.find_one("users", {"phone": normalized})
        if existing:
            raise ValueError("phone_already_registered")
        if referral_code:
            code = referral_code.strip().upper()
            referrer = self.db.find_one("users", {"referral_code": code})
            if not referrer:
                raise ValueError("invalid_referral_code")
        user_id = secrets.token_hex(16)
        user = {
            "user_id": user_id,
            "phone": normalized,
            "password_hash": hash_password(password),
            "status": "pending_verification",
            "role": "USER",
            "plan": "TRIAL",
            "demo_status": "active",
            "live_state": "not_started",
            "telegram_verified": False,
            "telegram_user_id": None,
            "referral_code": new_referral_code(self.db),
            "created_at": datetime.now(timezone.utc),
        }
        self.db.write("users", user)
        if referral_code:
            attach_referral(self.db, user_id, referral_code)
        challenge = new_token()
        self.db.write("registration_challenges", {
            "challenge_hash": token_hash(challenge),
            "user_id": user_id,
            "phone": normalized,
            "expires_at": expiry(1),
            "used": False,
        })
        return {"user_id": user_id, "challenge": challenge, "phone": normalized}

    def registration_challenge_info(self, challenge: str):
        ch = self.db.find_one("registration_challenges", {"challenge_hash": token_hash(challenge), "used": False})
        if not ch:
            return None
        expires_at = ch.get("expires_at")
        if expires_at and as_utc(expires_at) < datetime.now(timezone.utc):
            return None
        return {
            "challenge_hash": ch["challenge_hash"],
            "user_id": ch["user_id"],
            "phone": ch["phone"],
            "expires_at": expires_at,
        }

    def mark_telegram_contact(self, challenge: str, telegram_user_id: str, telegram_phone: str) -> bool:
        return self.mark_telegram_contact_by_hash(token_hash(challenge), telegram_user_id, telegram_phone)

    def mark_telegram_contact_by_hash(self, challenge_hash: str, telegram_user_id: str, telegram_phone: str) -> bool:
        ch = self.db.find_one("registration_challenges", {"challenge_hash": challenge_hash, "used": False})
        if not ch or as_utc(ch.get("expires_at") or datetime.now(timezone.utc)) < datetime.now(timezone.utc):
            return False
        if normalize_phone(telegram_phone) != ch["phone"]:
            return False
        self.db.upsert("telegram_verifications", {"challenge_hash": ch["challenge_hash"]}, {
            "challenge_hash": ch["challenge_hash"], "user_id": ch["user_id"],
            "telegram_user_id": str(telegram_user_id), "phone": ch["phone"],
            "verified": True, "verified_at": datetime.now(timezone.utc),
        })
        return True

    def verify_registration(self, challenge: str) -> bool:
        ch = self.db.find_one("registration_challenges", {"challenge_hash": token_hash(challenge), "used": False})
        if not ch or as_utc(ch.get("expires_at") or datetime.now(timezone.utc)) < datetime.now(timezone.utc):
            return False
        tv = self.db.find_one("telegram_verifications", {"challenge_hash": ch["challenge_hash"], "verified": True})
        if not tv or tv.get("phone") != ch["phone"]:
            return False
        self.db.upsert("users", {"user_id": ch["user_id"]}, {
            "status": "active", "telegram_verified": True,
            "telegram_user_id": tv["telegram_user_id"], "verified_at": datetime.now(timezone.utc)
        })
        self.db.upsert("registration_challenges", {"challenge_hash": ch["challenge_hash"]}, {"used": True})
        return True

    def login(self, phone: str, password: str, country_code: str = "") -> str:
        normalized = normalize_phone(phone, country_code)
        user = self.db.find_one("users", {"phone": normalized})
        if user and user.get("status") == "suspended":
            ban_until = user.get("ban_until")
            if ban_until and as_utc(ban_until) <= datetime.now(timezone.utc):
                self.db.upsert("users", {"user_id": user["user_id"]}, {"status": "active", "ban_until": None})
                user = self.db.find_one("users", {"phone": normalized})
        if not user or user.get("status") != "active" or not verify_password(password, user.get("password_hash", "")):
            raise ValueError("invalid_credentials")
        token = new_token()
        self.db.write("sessions", {
            "token_hash": token_hash(token), "user_id": user["user_id"],
            "expires_at": expiry(self.session_hours), "revoked": False,
            "created_at": datetime.now(timezone.utc),
        })
        return token

    def authenticate_token(self, token: str):
        """Used by admin-facing endpoints: returns the user or None, no exceptions."""
        if not token:
            return None
        session = self.db.find_one("sessions", {"token_hash": token_hash(token), "revoked": False})
        if not session or as_utc(session.get("expires_at") or datetime.now(timezone.utc)) <= datetime.now(timezone.utc):
            return None
        user = self.db.find_one("users", {"user_id": session.get("user_id")})
        if not user or user.get("status") != "active":
            return None
        return user

    def get_current_user(self, token: str):
        """Used by /auth/me, /auth/tutorial, /auth/logout and app/api/user_api.py."""
        session = self.db.find_one("sessions", {"token_hash": token_hash(token), "revoked": False})
        if not session or as_utc(session.get("expires_at") or datetime.now(timezone.utc)) <= datetime.now(timezone.utc):
            return None
        user = self.db.find_one("users", {"user_id": session.get("user_id")})
        if not user or user.get("status") != "active":
            return None
        return user

    def public_user(self, user: dict) -> dict:
        return {
            "user_id": user.get("user_id"),
            "phone": user.get("phone"),
            "status": user.get("status"),
            "role": user.get("role"),
            "plan": user.get("plan"),
            "telegram_verified": bool(user.get("telegram_verified")),
            "tutorial_completed": bool(user.get("tutorial_completed", False)),
        }

    def set_tutorial_completed(self, user_id: str, completed: bool = True):
        self.db.upsert("users", {"user_id": user_id}, {"tutorial_completed": bool(completed)})

    def revoke_session(self, token: str) -> bool:
        session = self.db.find_one("sessions", {"token_hash": token_hash(token), "revoked": False})
        if not session:
            return False
        self.db.upsert("sessions", {"token_hash": token_hash(token)}, {"revoked": True, "revoked_at": datetime.now(timezone.utc)})
        return True
