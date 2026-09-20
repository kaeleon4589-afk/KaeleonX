from app.auth.service import AuthService
from app.storage.database import Database
from app.telegram.bot import TelegramBotService
import app.auth.service as auth_module

auth_module.hash_password = lambda password: "test$" + password
auth_module.verify_password = lambda password, encoded: encoded == "test$" + password


def test_telegram_registration_flow_with_self_contact():
    db = Database()
    auth = AuthService(db)
    reg = auth.create_registration("5554444444", "StrongPass123!", "+1")

    bot = TelegramBotService(
        auth,
        db,
        token="fake-token",
        username="KAELEONBot",
        webhook_url="https://example.com/telegram/webhook",
        webhook_secret="test-secret",
    )
    sent = []

    async def fake_send(chat_id, text, reply_markup=None):
        sent.append((str(chat_id), text, reply_markup))
        return {"ok": True}

    bot.send_message = fake_send
    bot.clear_keyboard = fake_send

    import asyncio

    asyncio.run(bot.handle_update({
        "message": {
            "chat": {"id": 1001},
            "from": {"id": 2002},
            "text": f"/start {reg['challenge']}",
        }
    }))

    assert sent
    assert sent[-1][2]["keyboard"][0][0]["request_contact"] is True

    asyncio.run(bot.handle_update({
        "message": {
            "chat": {"id": 1001},
            "from": {"id": 2002},
            "contact": {"user_id": 2002, "phone_number": "+15554444444"},
        }
    }))

    assert auth.verify_registration(reg["challenge"]) is True
    user = db.find_one("users", {"user_id": reg["user_id"]})
    assert user["status"] == "active"
    assert user["telegram_verified"] is True
    assert user["telegram_user_id"] == "2002"


def test_forwarded_or_other_user_contact_is_rejected():
    db = Database()
    auth = AuthService(db)
    reg = auth.create_registration("5555555555", "StrongPass123!", "+1")

    bot = TelegramBotService(
        auth,
        db,
        token="fake-token",
        username="KAELEONBot",
    )
    sent = []

    async def fake_send(chat_id, text, reply_markup=None):
        sent.append(text)
        return {"ok": True}

    bot.send_message = fake_send
    bot.clear_keyboard = fake_send

    import asyncio

    asyncio.run(bot.handle_update({
        "message": {
            "chat": {"id": 3001},
            "from": {"id": 4002},
            "text": f"/start {reg['challenge']}",
        }
    }))
    asyncio.run(bot.handle_update({
        "message": {
            "chat": {"id": 3001},
            "from": {"id": 4002},
            "contact": {"user_id": 9999, "phone_number": "+15555555555"},
        }
    }))

    assert auth.verify_registration(reg["challenge"]) is False
    assert any("propio número" in text for text in sent)
