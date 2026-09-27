import hashlib
import hmac
import secrets
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.referrals import new_referral_code, attach_referral

E164_MAX = 15
SCRYPT_MAXMEM = 64 * 1024 * 1024

RECOVERY_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
RECOVERY_CODE_GROUPS = 5
RECOVERY_CODE_GROUP_SIZE = 4
PASSWORD_RECOVERY_OTP_TTL_MINUTES = 10
PASSWORD_RECOVERY_MAX_ATTEMPTS = 5
PASSWORD_RECOVERY_REQUEST_WINDOW_MINUTES = 15
PASSWORD_RECOVERY_REQUEST_MAX = 3
PASSWORD_RECOVERY_FAILURE_WINDOW_MINUTES = 15
PASSWORD_RECOVERY_FAILURE_MAX = 5


def hash_recovery_secret(secret: str) -> str:
    value = (secret or "").strip().upper().encode()
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(value, salt=salt, n=2**14, r=8, p=1, maxmem=SCRYPT_MAXMEM)
    return f"scrypt-recovery$16384$8$1${salt.hex()}${digest.hex()}"


def verify_recovery_secret(secret: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "scrypt-recovery":
            return False
        digest = hashlib.scrypt(
            (secret or "").strip().upper().encode(),
            salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), maxmem=SCRYPT_MAXMEM,
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def new_recovery_code() -> str:
    groups = []
    for _ in range(RECOVERY_CODE_GROUPS):
        groups.append("".join(secrets.choice(RECOVERY_CODE_ALPHABET) for _ in range(RECOVERY_CODE_GROUP_SIZE)))
    return "KAE-" + "-".join(groups)


def valid_recovery_code_format(code: str) -> bool:
    return bool(re.fullmatch(r"KAE(?:-[A-HJ-NP-Z2-9]{4}){5}", (code or "").strip().upper()))


def fingerprint(value: str) -> str:
    return hashlib.sha256((value or "").encode()).hexdigest()


# Used to keep recovery-code verification work approximately constant even when
# a phone does not belong to an account. The value is random per process and is
# never a valid user credential.
DUMMY_RECOVERY_HASH = hash_recovery_secret("KAE-AAAA-AAAA-AAAA-AAAA-AAAA")


def normalize_phone(phone: str, country_code: str = "") -> str:
    raw = (phone or "").strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if not raw:
        raise ValueError("phone_required")

    # Telegram's Bot API commonly sends contact.phone_number as digits only
    # (for example ``5359494299``) even when the registered E.164 value is
    # ``+5359494299``.  When no explicit country code is supplied, a digits-only
    # value is therefore treated as an already-complete international number.
    if raw.startswith("+"):
        digits = raw[1:]
    else:
        cc = (country_code or "").strip().replace(" ", "").replace("-", "")
        cc_digits = cc.lstrip("+")
        if cc_digits:
            if not cc_digits.isdigit():
                raise ValueError("invalid_phone")
            digits = cc_digits + raw.lstrip("+")
        else:
            digits = raw.lstrip("+")

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

        if referral_code:
            code = referral_code.strip().upper()
            referrer = self.db.find_one("users", {"referral_code": code})
            if not referrer:
                raise ValueError("invalid_referral_code")

        # A failed/abandoned Telegram verification must not permanently lock the
        # phone number.  Active (or suspended) accounts remain protected, while
        # a pending account can safely restart registration and receive a fresh
        # one-hour challenge.
        if existing:
            if existing.get("status") != "pending_verification":
                raise ValueError("phone_already_registered")
            user_id = existing["user_id"]
            self.db.upsert("users", {"user_id": user_id}, {
                "password_hash": hash_password(password),
                "telegram_verified": False,
                "telegram_user_id": None,
            })
            self.db.update_many(
                "registration_challenges",
                {"user_id": user_id, "used": False},
                {"used": True, "invalidated_at": datetime.now(timezone.utc)},
            )
            if referral_code and not existing.get("referred_by_user_id"):
                attach_referral(self.db, user_id, referral_code)
        else:
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

    def mark_telegram_contact(self, challenge: str, telegram_user_id: str, telegram_phone: str, telegram_chat_id: str | None = None) -> bool:
        return self.mark_telegram_contact_by_hash(token_hash(challenge), telegram_user_id, telegram_phone, telegram_chat_id)

    def mark_telegram_contact_by_hash(self, challenge_hash: str, telegram_user_id: str, telegram_phone: str, telegram_chat_id: str | None = None) -> bool:
        ch = self.db.find_one("registration_challenges", {"challenge_hash": challenge_hash, "used": False})
        if not ch or as_utc(ch.get("expires_at") or datetime.now(timezone.utc)) < datetime.now(timezone.utc):
            return False
        if normalize_phone(telegram_phone) != ch["phone"]:
            return False
        self.db.upsert("telegram_verifications", {"challenge_hash": ch["challenge_hash"]}, {
            "challenge_hash": ch["challenge_hash"], "user_id": ch["user_id"],
            "telegram_user_id": str(telegram_user_id), "telegram_chat_id": str(telegram_chat_id or telegram_user_id), "phone": ch["phone"],
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
            "telegram_user_id": tv["telegram_user_id"],
            "telegram_chat_id": tv.get("telegram_chat_id") or tv["telegram_user_id"],
            "verified_at": datetime.now(timezone.utc)
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
            "recovery_code_required": not bool(user.get("recovery_code_hash")),
        }

    def enroll_recovery_code(self, user_id: str, code: str) -> dict:
        user = self.db.find_one("users", {"user_id": user_id})
        if not user or user.get("status") != "active":
            raise ValueError("invalid_user")
        if user.get("recovery_code_hash"):
            raise ValueError("recovery_code_already_configured")
        normalized = (code or "").strip().upper()
        if not valid_recovery_code_format(normalized):
            raise ValueError("invalid_recovery_code_format")
        now = datetime.now(timezone.utc)
        self.db.upsert("users", {"user_id": user_id}, {
            "recovery_code_hash": hash_recovery_secret(normalized),
            "recovery_code_created_at": now,
            "recovery_code_used_at": None,
        })
        self._security_event(user_id, "RECOVERY_CODE_ENROLLED")
        return {"configured": True, "created_at": now}

    def regenerate_recovery_code(self, user_id: str, current_password: str) -> dict:
        user = self.db.find_one("users", {"user_id": user_id})
        if not user or user.get("status") != "active":
            raise ValueError("invalid_user")
        if not verify_password(current_password, user.get("password_hash", "")):
            raise ValueError("current_password_invalid")
        code = self._rotate_recovery_code(user_id)
        self._security_event(user_id, "RECOVERY_CODE_REGENERATED")
        return {
            "recovery_code": code,
            "telegram_chat_id": user.get("telegram_chat_id") or user.get("telegram_user_id"),
        }

    def _security_event(self, user_id: str | None, event: str, **fields) -> None:
        payload = {"event": event, "created_at": datetime.now(timezone.utc), **fields}
        if user_id:
            payload["user_id"] = user_id
        self.db.write("security_events", payload)

    def _recent_rows(self, collection: str, key: dict, since: datetime) -> list[dict]:
        rows = self.db.find_many(collection, key, limit=1000, sort_field="created_at", descending=True)
        return [r for r in rows if as_utc(r.get("created_at")) and as_utc(r.get("created_at")) >= since]

    def _recovery_rate_limited(self, phone_fp: str, requester_ip: str) -> bool:
        since = datetime.now(timezone.utc) - timedelta(minutes=PASSWORD_RECOVERY_REQUEST_WINDOW_MINUTES)
        phone_rows = self._recent_rows("password_recovery_requests", {"phone_fingerprint": phone_fp}, since)
        ip_rows = self._recent_rows("password_recovery_requests", {"requester_ip": requester_ip}, since) if requester_ip else []
        return len(phone_rows) >= PASSWORD_RECOVERY_REQUEST_MAX or len(ip_rows) >= PASSWORD_RECOVERY_REQUEST_MAX * 4

    def begin_telegram_password_recovery(self, phone: str, country_code: str = "", requester_ip: str = "") -> dict:
        normalized = normalize_phone(phone, country_code)
        phone_fp = fingerprint(normalized)
        now = datetime.now(timezone.utc)
        limited = self._recovery_rate_limited(phone_fp, requester_ip)
        self.db.write("password_recovery_requests", {
            "phone_fingerprint": phone_fp,
            "requester_ip": requester_ip,
            "method": "telegram",
            "rate_limited": limited,
            "created_at": now,
        })
        public_challenge = new_token()
        if limited:
            return {"challenge": public_challenge, "deliver": False}

        user = self.db.find_one("users", {"phone": normalized})
        chat_id = (user or {}).get("telegram_chat_id") or (user or {}).get("telegram_user_id")
        if not user or user.get("status") != "active" or not user.get("telegram_verified") or not chat_id:
            return {"challenge": public_challenge, "deliver": False}

        otp = f"{secrets.randbelow(1_000_000):06d}"
        self.db.update_many("password_recovery_challenges", {"user_id": user["user_id"], "used": False}, {
            "used": True, "invalidated_at": now, "invalidated_reason": "superseded"
        })
        self.db.write("password_recovery_challenges", {
            "challenge_hash": token_hash(public_challenge),
            "user_id": user["user_id"],
            "otp_hash": hash_recovery_secret(otp),
            "expires_at": now + timedelta(minutes=PASSWORD_RECOVERY_OTP_TTL_MINUTES),
            "attempts": 0,
            "used": False,
            "requester_ip": requester_ip,
            "created_at": now,
        })
        self._security_event(user["user_id"], "PASSWORD_RECOVERY_TELEGRAM_REQUESTED", requester_ip=requester_ip)
        return {
            "challenge": public_challenge,
            "deliver": True,
            "otp": otp,
            "telegram_chat_id": str(chat_id),
            "user_id": user["user_id"],
        }

    def _rotate_recovery_code(self, user_id: str, used_at: datetime | None = None) -> str:
        code = new_recovery_code()
        now = datetime.now(timezone.utc)
        update = {
            "recovery_code_hash": hash_recovery_secret(code),
            "recovery_code_created_at": now,
            "recovery_code_used_at": None,
            "recovery_code_rotated_at": now,
        }
        if used_at is not None:
            update["last_recovery_code_used_at"] = used_at
        self.db.upsert("users", {"user_id": user_id}, update)
        return code

    def _revoke_all_sessions(self, user_id: str, reason: str) -> None:
        self.db.update_many("sessions", {"user_id": user_id, "revoked": False}, {
            "revoked": True,
            "revoked_at": datetime.now(timezone.utc),
            "revocation_reason": reason,
        })

    def _set_recovered_password(self, user_id: str, new_password: str, method: str, requester_ip: str = "") -> str:
        now = datetime.now(timezone.utc)
        # hash_password enforces the same password policy used by registration.
        password_hash = hash_password(new_password)
        self.db.upsert("users", {"user_id": user_id}, {
            "password_hash": password_hash,
            "password_changed_at": now,
            "last_password_recovery_method": method,
        })
        self._revoke_all_sessions(user_id, "password_recovery")
        new_code = self._rotate_recovery_code(user_id, used_at=now)
        self._security_event(user_id, "PASSWORD_RESET_COMPLETED", method=method, requester_ip=requester_ip)
        return new_code

    def complete_telegram_password_recovery(self, challenge: str, otp: str, new_password: str, requester_ip: str = "") -> dict:
        ch = self.db.find_one("password_recovery_challenges", {
            "challenge_hash": token_hash(challenge), "used": False
        })
        now = datetime.now(timezone.utc)
        if not ch:
            verify_recovery_secret((otp or "").strip(), DUMMY_RECOVERY_HASH)
            raise ValueError("invalid_or_expired_recovery_challenge")
        if as_utc(ch.get("expires_at") or now) <= now:
            raise ValueError("invalid_or_expired_recovery_challenge")
        attempts = int(ch.get("attempts", 0))
        if attempts >= PASSWORD_RECOVERY_MAX_ATTEMPTS:
            self.db.upsert("password_recovery_challenges", {"challenge_hash": ch["challenge_hash"]}, {
                "used": True, "invalidated_at": now, "invalidated_reason": "too_many_attempts"
            })
            raise ValueError("invalid_or_expired_recovery_challenge")
        if not verify_recovery_secret((otp or "").strip(), ch.get("otp_hash", "")):
            attempts += 1
            update = {"attempts": attempts, "last_failed_at": now}
            if attempts >= PASSWORD_RECOVERY_MAX_ATTEMPTS:
                update.update({"used": True, "invalidated_at": now, "invalidated_reason": "too_many_attempts"})
            self.db.upsert("password_recovery_challenges", {"challenge_hash": ch["challenge_hash"]}, update)
            self._security_event(ch.get("user_id"), "PASSWORD_RECOVERY_OTP_FAILED", requester_ip=requester_ip)
            raise ValueError("invalid_recovery_code")

        self.db.upsert("password_recovery_challenges", {"challenge_hash": ch["challenge_hash"]}, {
            "used": True, "used_at": now, "attempts": attempts
        })
        new_code = self._set_recovered_password(ch["user_id"], new_password, "telegram", requester_ip)
        user = self.db.find_one("users", {"user_id": ch["user_id"]}) or {}
        return {
            "reset": True,
            "recovery_code": new_code,
            "telegram_chat_id": user.get("telegram_chat_id"),
            "user_id": ch["user_id"],
        }

    def _recovery_code_failure_limited(self, phone_fp: str, requester_ip: str) -> bool:
        since = datetime.now(timezone.utc) - timedelta(minutes=PASSWORD_RECOVERY_FAILURE_WINDOW_MINUTES)
        phone_rows = [r for r in self._recent_rows("password_recovery_attempts", {"phone_fingerprint": phone_fp}, since) if not r.get("success")]
        ip_rows = [r for r in self._recent_rows("password_recovery_attempts", {"requester_ip": requester_ip}, since) if not r.get("success")] if requester_ip else []
        return len(phone_rows) >= PASSWORD_RECOVERY_FAILURE_MAX or len(ip_rows) >= PASSWORD_RECOVERY_FAILURE_MAX * 4

    def reset_password_with_recovery_code(self, phone: str, recovery_code: str, new_password: str, country_code: str = "", requester_ip: str = "") -> dict:
        normalized = normalize_phone(phone, country_code)
        phone_fp = fingerprint(normalized)
        if self._recovery_code_failure_limited(phone_fp, requester_ip):
            raise ValueError("recovery_temporarily_locked")
        user = self.db.find_one("users", {"phone": normalized})
        stored_hash = (user or {}).get("recovery_code_hash") or DUMMY_RECOVERY_HASH
        format_ok = valid_recovery_code_format(recovery_code)
        secret_ok = verify_recovery_secret(recovery_code if format_ok else "KAE-AAAA-AAAA-AAAA-AAAA-AAAA", stored_hash)
        valid = bool(
            user
            and user.get("status") == "active"
            and user.get("recovery_code_hash")
            and format_ok
            and secret_ok
        )
        self.db.write("password_recovery_attempts", {
            "phone_fingerprint": phone_fp,
            "requester_ip": requester_ip,
            "method": "recovery_code",
            "success": valid,
            "created_at": datetime.now(timezone.utc),
        })
        if not valid:
            if user:
                self._security_event(user.get("user_id"), "PASSWORD_RECOVERY_CODE_FAILED", requester_ip=requester_ip)
            raise ValueError("invalid_recovery_credentials")
        new_code = self._set_recovered_password(user["user_id"], new_password, "recovery_code", requester_ip)
        return {
            "reset": True,
            "recovery_code": new_code,
            "telegram_chat_id": user.get("telegram_chat_id"),
            "user_id": user["user_id"],
        }

    def set_tutorial_completed(self, user_id: str, completed: bool = True):
        self.db.upsert("users", {"user_id": user_id}, {"tutorial_completed": bool(completed)})

    def revoke_session(self, token: str) -> bool:
        session = self.db.find_one("sessions", {"token_hash": token_hash(token), "revoked": False})
        if not session:
            return False
        self.db.upsert("sessions", {"token_hash": token_hash(token)}, {"revoked": True, "revoked_at": datetime.now(timezone.utc)})
        return True
