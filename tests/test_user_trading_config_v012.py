import asyncio

from fastapi.testclient import TestClient
from cryptography.fernet import Fernet

from app.api.app import app
from app.api import auth_api, user_api
from app.auth.service import AuthService
import app.auth.service as auth_module
from app.config import settings as settings_module
import app.api.user_api as user_api_module
from app.storage.database import Database
from app.security.credential_vault import CredentialVault
from app.trading.profile import UserTradingProfileService
from app.billing.service import BillingService


auth_module.hash_password = lambda password: "test$" + password
auth_module.verify_password = lambda password, encoded: encoded == "test$" + password
TEST_SETTINGS = None


def setup_function():
    global TEST_SETTINGS
    auth_api._db = None
    auth_api._service = None
    user_api._db = None
    user_api._auth = None
    user_api._profiles = None
    user_api._billing = None
    settings = settings_module.Settings(
        environment="test",
        mongodb_uri="",
        credential_encryption_key=Fernet.generate_key().decode(),
        telegram_enabled=False,
    )
    TEST_SETTINGS = settings
    auth_api.get_settings = lambda: settings
    user_api_module.get_settings = lambda: settings
    async def fake_equity(profiles, user_id):
        return 100.0
    user_api_module._coinw_equity = fake_equity


def register_and_login(phone):
    c = TestClient(app)
    r = c.post('/auth/register', json={'phone': phone, 'country_code': '+1', 'password': 'StrongPass123!'})
    assert r.status_code == 200
    user = auth_api._db.find_one('users', {'phone': r.json()['phone']})
    auth_api._db.upsert('users', {'user_id': user['user_id']}, {'status': 'active', 'telegram_verified': True})
    r = c.post('/auth/login', json={'phone': phone, 'country_code': '+1', 'password': 'StrongPass123!'})
    assert r.status_code == 200
    user_api_module._db = auth_api._db
    user_api_module._auth = AuthService(auth_api._db)
    user_api_module._profiles = UserTradingProfileService(auth_api._db, CredentialVault(TEST_SETTINGS.credential_encryption_key), minimum_operating_capital=TEST_SETTINGS.min_operating_capital)
    user_api_module._billing = BillingService(auth_api._db, trial_days=TEST_SETTINGS.live_trial_days)
    return c, r.json()['access_token'], user['user_id']


def test_user_coinw_credentials_and_capital_are_isolated_and_secret_never_returned():
    c1, t1, u1 = register_and_login('5551010101')
    c2, t2, u2 = register_and_login('5552020202')
    h1 = {'Authorization': f'Bearer {t1}'}
    h2 = {'Authorization': f'Bearer {t2}'}

    # LIVE is still entitlement-gated.
    r = c1.put('/user/trading-config', headers=h1, json={
        'execution_mode': 'live', 'trading_enabled': True,
        'coinw_api_key': 'key-user-1-123456', 'coinw_api_secret': 'secret-user-1-abcdef',
    })
    assert r.status_code == 403

    # Credentials are saved first, then explicitly verified, then capital/trading.
    r = c1.put('/user/trading-config', headers=h1, json={
        'execution_mode': 'demo', 'trading_enabled': False,
        'coinw_api_key': 'key-user-1-123456', 'coinw_api_secret': 'secret-user-1-abcdef',
    })
    assert r.status_code == 200
    user_api_module._profiles.mark_verified(u1, 100.0)
    r = c1.put('/user/trading-config', headers=h1, json={
        'execution_mode': 'demo', 'trading_enabled': True, 'operating_capital': 12,
    })
    assert r.status_code == 200
    body = r.json()
    assert body['operating_capital'] == 12
    assert body['coinw_configured'] is True
    assert 'secret-user-1' not in str(body)

    r = c1.get('/user/trading-config', headers=h1)
    assert r.status_code == 200
    body = r.json()
    assert body['coinw_configured'] is True
    assert body['coinw_api_key'].startswith('key-')
    assert 'secret-user-1' not in str(body)

    r = c2.get('/user/trading-config', headers=h2)
    assert r.status_code == 200
    assert r.json()['coinw_configured'] is False

    stored = auth_api._db.find_one('user_trading_profiles', {'user_id': u1})
    assert stored['coinw_api_key_encrypted'] != 'key-user-1-123456'
    assert stored['coinw_api_secret_encrypted'] != 'secret-user-1-abcdef'
    assert auth_api._db.find_one('user_trading_profiles', {'user_id': u2}) is None

def test_operating_capital_minimum_is_enforced():
    c, token, _ = register_and_login('5553030303')
    r = c.put('/user/trading-config', headers={'Authorization': f'Bearer {token}'}, json={
        'execution_mode': 'demo', 'trading_enabled': True, 'operating_capital': 2.99,
    })
    assert r.status_code == 400
    assert r.json()['detail'] == 'operating_capital_below_platform_minimum'


def test_live_credentials_cannot_change_or_switch_while_open_position_exists():
    c, token, uid = register_and_login('5554040404')
    h = {'Authorization': f'Bearer {token}'}
    db = auth_api._db
    billing = BillingService(db, trial_days=TEST_SETTINGS.live_trial_days)
    billing.activate_live_first_time(uid)
    r = c.put('/user/trading-config', headers=h, json={
        'execution_mode': 'live', 'trading_enabled': False,
        'coinw_api_key': 'key-12345678', 'coinw_api_secret': 'secret-12345678',
    })
    assert r.status_code == 200
    user_api_module._profiles.mark_verified(uid, 100.0)
    r = c.put('/user/trading-config', headers=h, json={
        'execution_mode': 'live', 'trading_enabled': False, 'operating_capital': 10,
    })
    assert r.status_code == 200
    db.upsert('positions', {'position_id': 'p-live-1'}, {'user_id': uid, 'status': 'OPEN', 'symbol': 'BTC', 'mode': 'live'})
    r = c.put('/user/trading-config', headers=h, json={
        'execution_mode': 'demo', 'trading_enabled': False, 'operating_capital': 10,
    })
    assert r.status_code == 400
    assert r.json()['detail'] == 'close_live_position_before_switching_to_demo'
    r = c.put('/user/trading-config', headers=h, json={
        'execution_mode': 'live', 'trading_enabled': False, 'operating_capital': 10,
        'coinw_api_key': 'new-key-12345678', 'coinw_api_secret': 'new-secret-12345678',
    })
    assert r.status_code == 400
    assert r.json()['detail'] == 'disable_live_trading_and_close_position_before_changing_credentials'
