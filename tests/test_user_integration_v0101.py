from fastapi.testclient import TestClient
from app.api.app import app
from app.api import auth_api, user_api
from app.auth.service import AuthService
from app.telegram.bot import TelegramBotService
from app.storage.database import Database
from cryptography.fernet import Fernet
from app.config import settings as settings_module
import app.auth.service as auth_module
import asyncio

auth_module.hash_password = lambda password: "test$" + password
auth_module.verify_password = lambda password, encoded: encoded == "test$" + password


def setup_function():
    auth_api._db = None; auth_api._service = None
    user_api._db = None; user_api._auth = None; user_api._profiles = None; user_api._billing = None
    settings = settings_module.Settings(environment="test", mongodb_uri="", credential_encryption_key=Fernet.generate_key().decode(), telegram_enabled=False)
    auth_api.get_settings = lambda: settings
    user_api.get_settings = lambda: settings


def register_and_login(phone, telegram_id):
    c = TestClient(app)
    r = c.post('/auth/register', json={'phone': phone, 'country_code': '+1', 'password': 'StrongPass123!'})
    assert r.status_code == 200
    ch = r.json()['challenge']
    db = auth_api._db
    auth = AuthService(db)
    user_api._db = db
    user_api._auth = auth
    bot = TelegramBotService(auth, db, token='fake-token', username='KAELEONBot')
    async def fake_send(chat_id, text, reply_markup=None):
        return {'ok': True}
    bot.send_message = fake_send
    bot.clear_keyboard = fake_send
    asyncio.run(bot.handle_update({'message': {'chat': {'id': telegram_id}, 'from': {'id': telegram_id}, 'text': f'/start {ch}'}}))
    asyncio.run(bot.handle_update({'message': {'chat': {'id': telegram_id}, 'from': {'id': telegram_id}, 'contact': {'user_id': telegram_id, 'phone_number': '+1' + phone.lstrip('+')}}}))
    assert c.post('/auth/verify', json={'challenge': ch}).status_code == 200
    r = c.post('/auth/login', json={'phone': phone, 'country_code': '+1', 'password': 'StrongPass123!'})
    return c, r.json()['access_token']


def test_full_user_flow_and_isolation():
    c1, t1 = register_and_login('5551111111', 'tg-1')
    c2, t2 = register_and_login('5552222222', 'tg-2')
    assert c1.get('/auth/me', headers={'Authorization': f'Bearer {t1}'}).json()['phone'] == '+15551111111'
    assert c1.get('/user/dashboard', headers={'Authorization': f'Bearer {t1}'}).status_code == 200
    assert c1.get('/user/execution', headers={'Authorization': f'Bearer {t1}'}).json()['market_selection'] == 'KAELEON_AUTO'
    assert c1.get('/user/settings', headers={'Authorization': f'Bearer {t1}'}).json()['timeframes'] == 'INTERNAL_MTF_BY_STRATEGY'
    assert c1.get('/user/operations', headers={'Authorization': f'Bearer {t1}'}).json()['open'] == []
    assert c2.get('/user/dashboard', headers={'Authorization': f'Bearer {t2}'}).json()['user']['user_id'] != c1.get('/auth/me', headers={'Authorization': f'Bearer {t1}'}).json()['user_id']


def test_logout_blocks_session():
    c, token = register_and_login('5553333333', 'tg-3')
    h={'Authorization': f'Bearer {token}'}
    assert c.post('/auth/logout', headers=h).status_code == 200
    assert c.get('/user/dashboard', headers=h).status_code == 401
