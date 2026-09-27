from app.auth.service import AuthService, new_recovery_code, valid_recovery_code_format
import app.auth.service as auth_module
from app.storage.database import Database


def active_user(db: Database, phone: str = "+15551234567", password: str = "StrongPass123!") -> dict:
    user = {
        "user_id": "user-recovery-1",
        "phone": phone,
        "password_hash": auth_module.hash_password(password),
        "status": "active",
        "role": "USER",
        "plan": "TRIAL",
        "telegram_verified": True,
        "telegram_user_id": "9001",
        "telegram_chat_id": "9001",
    }
    db.write("users", user)
    return user


def test_existing_user_requires_recovery_code_until_enrolled():
    db = Database()
    auth = AuthService(db)
    user = active_user(db)

    assert auth.public_user(user)["recovery_code_required"] is True
    code = "KAE-ABCD-EFGH-JKMN-PQRS-TUVW"
    assert valid_recovery_code_format(code)
    auth.enroll_recovery_code(user["user_id"], code)

    stored = db.find_one("users", {"user_id": user["user_id"]})
    assert stored["recovery_code_hash"] != code
    assert code not in str(stored)
    assert auth.public_user(stored)["recovery_code_required"] is False


def test_recovery_code_reset_revokes_sessions_and_rotates_code():
    db = Database()
    auth = AuthService(db)
    user = active_user(db)
    old_code = "KAE-ABCD-EFGH-JKMN-PQRS-TUVW"
    auth.enroll_recovery_code(user["user_id"], old_code)

    token = auth.login(user["phone"], "StrongPass123!")
    assert auth.get_current_user(token) is not None

    result = auth.reset_password_with_recovery_code(
        user["phone"], old_code, "NewStrongPass456!", requester_ip="203.0.113.7"
    )
    assert result["reset"] is True
    assert result["recovery_code"] != old_code
    assert valid_recovery_code_format(result["recovery_code"])
    assert auth.get_current_user(token) is None

    # A recovery credential is one-use: the old code cannot reset the account again.
    try:
        auth.reset_password_with_recovery_code(user["phone"], old_code, "AnotherPass789!")
        assert False, "old recovery code must be invalid after reset"
    except ValueError as exc:
        assert str(exc) == "invalid_recovery_credentials"

    # The rotated code is the only valid recovery code now.
    second = auth.reset_password_with_recovery_code(
        user["phone"], result["recovery_code"], "AnotherPass789!"
    )
    assert second["reset"] is True
    assert second["recovery_code"] != result["recovery_code"]


def test_telegram_otp_reset_is_one_time_and_revokes_sessions():
    db = Database()
    auth = AuthService(db)
    user = active_user(db)
    auth.enroll_recovery_code(user["user_id"], "KAE-ABCD-EFGH-JKMN-PQRS-TUVW")
    token = auth.login(user["phone"], "StrongPass123!")

    started = auth.begin_telegram_password_recovery(user["phone"], requester_ip="198.51.100.9")
    assert started["deliver"] is True
    assert len(started["otp"]) == 6 and started["otp"].isdigit()
    assert started["telegram_chat_id"] == "9001"

    result = auth.complete_telegram_password_recovery(
        started["challenge"], started["otp"], "TelegramReset456!", requester_ip="198.51.100.9"
    )
    assert result["reset"] is True
    assert valid_recovery_code_format(result["recovery_code"])
    assert auth.get_current_user(token) is None

    try:
        auth.complete_telegram_password_recovery(
            started["challenge"], started["otp"], "ShouldNotWork123!"
        )
        assert False, "OTP challenge must be one-use"
    except ValueError as exc:
        assert str(exc) == "invalid_or_expired_recovery_challenge"


def test_telegram_recovery_never_exposes_account_existence_in_request_shape():
    db = Database()
    auth = AuthService(db)
    result = auth.begin_telegram_password_recovery("+15559999999", requester_ip="192.0.2.4")
    assert result["deliver"] is False
    assert isinstance(result["challenge"], str) and len(result["challenge"]) >= 20


def test_recovery_code_generator_uses_expected_format_and_entropy_shape():
    codes = {new_recovery_code() for _ in range(20)}
    assert len(codes) == 20
    assert all(valid_recovery_code_format(code) for code in codes)


def test_password_recovery_api_telegram_round_trip(monkeypatch):
    import re
    from fastapi.testclient import TestClient
    from app.api.app import app
    from app.api import auth_api

    db = Database()
    auth = AuthService(db)
    user = active_user(db, phone="+15557654321")
    auth.enroll_recovery_code(user["user_id"], "KAE-ABCD-EFGH-JKMN-PQRS-TUVW")
    auth_api._db = db
    auth_api._service = auth
    sent = []

    async def fake_security_message(chat_id, text):
        sent.append((str(chat_id), text))
        return True

    monkeypatch.setattr(auth_api, "send_security_message", fake_security_message)
    client = TestClient(app)
    requested = client.post('/auth/password-recovery/telegram/request', json={
        'phone': '+15557654321', 'country_code': ''
    })
    assert requested.status_code == 200
    body = requested.json()
    assert body['accepted'] is True
    assert 'challenge' in body
    assert sent and sent[0][0] == '9001'
    otp_match = re.search(r"(\d{6})", sent[0][1])
    assert otp_match

    confirmed = client.post('/auth/password-recovery/telegram/confirm', json={
        'challenge': body['challenge'],
        'otp': otp_match.group(1),
        'new_password': 'RoundTripReset123!',
    })
    assert confirmed.status_code == 200
    assert valid_recovery_code_format(confirmed.json()['recovery_code'])
    assert len(sent) == 2  # OTP + password-changed security alert


def test_telegram_otp_locks_after_five_failed_attempts():
    db = Database()
    auth = AuthService(db)
    user = active_user(db, phone="+15550001111")
    started = auth.begin_telegram_password_recovery(user["phone"], requester_ip="198.51.100.22")
    assert started["deliver"] is True

    for _ in range(5):
        try:
            auth.complete_telegram_password_recovery(started["challenge"], "000000", "ResetPass123!")
        except ValueError:
            pass

    challenge = db.find_one("password_recovery_challenges", {"user_id": user["user_id"]})
    assert challenge["used"] is True
    assert challenge["invalidated_reason"] == "too_many_attempts"


def test_telegram_recovery_request_rate_limit_stops_fourth_delivery():
    db = Database()
    auth = AuthService(db)
    user = active_user(db, phone="+15550002222")
    results = [auth.begin_telegram_password_recovery(user["phone"], requester_ip="203.0.113.44") for _ in range(4)]
    assert [r["deliver"] for r in results] == [True, True, True, False]


def test_admin_serialization_never_exposes_recovery_hash():
    from app.api.admin_api import clean
    doc = {"user_id": "u1", "password_hash": "secret", "recovery_code_hash": "recovery-secret", "status": "active"}
    result = clean(doc)
    assert "password_hash" not in result
    assert "recovery_code_hash" not in result
    assert result["user_id"] == "u1"


def test_authenticated_user_can_regenerate_recovery_code_with_current_password():
    db = Database()
    auth = AuthService(db)
    user = active_user(db, phone="+15550003333")
    old_code = "KAE-ABCD-EFGH-JKMN-PQRS-TUVW"
    auth.enroll_recovery_code(user["user_id"], old_code)

    result = auth.regenerate_recovery_code(user["user_id"], "StrongPass123!")
    assert valid_recovery_code_format(result["recovery_code"])
    assert result["recovery_code"] != old_code

    try:
        auth.regenerate_recovery_code(user["user_id"], "WrongPassword123!")
        assert False, "wrong current password must not regenerate recovery code"
    except ValueError as exc:
        assert str(exc) == "current_password_invalid"
