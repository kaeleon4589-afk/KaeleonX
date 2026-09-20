from datetime import datetime, timedelta, timezone
import pytest

from app.storage.database import Database
from app.billing import BillingService
from app.auth.service import AuthService


def seed(db, user_id, referral_code=None):
    d = {'user_id': user_id, 'phone': '+' + '1' * 11, 'status': 'active'}
    if referral_code:
        d['referral_code'] = referral_code
    db.write('users', d)


def test_referral_reward_extends_same_live_entitlement_once():
    db = Database()
    seed(db, 'ref', 'KAELEON-REF')
    seed(db, 'buyer')
    db.upsert('users', {'user_id': 'buyer'}, {'referred_by_user_id': 'ref'})
    svc = BillingService(db)
    order = svc.create_payment_order('buyer', '15D', '0xreceiver')
    tx = '0x' + 'a' * 64
    db.upsert('payment_orders', {'payment_order_id': order['payment_order_id']}, {'tx_hash': tx, 'status': 'VERIFYING'})

    class V:
        def verify(self, order, tx_hash): return {'valid': True, 'status': 'CONFIRMED', 'block_number': 1}

    svc.confirm_payment(order['payment_order_id'], tx, V())
    ref = db.find_one('users', {'user_id': 'ref'})
    assert ref['referral_reward_days_total'] == 7
    assert ref['live_state'] == 'subscribed'

    order2 = svc.create_payment_order('buyer', '30D', '0xreceiver')
    tx2 = '0x' + 'b' * 64
    db.upsert('payment_orders', {'payment_order_id': order2['payment_order_id']}, {'tx_hash': tx2, 'status': 'VERIFYING'})
    svc.confirm_payment(order2['payment_order_id'], tx2, V())
    ref2 = db.find_one('users', {'user_id': 'ref'})
    assert ref2['referral_reward_days_total'] == 7


def test_expired_payment_order_cannot_be_submitted():
    db = Database(); seed(db, 'u2')
    svc = BillingService(db)
    order = svc.create_payment_order('u2', '30D', '0xreceiver')
    db.upsert('payment_orders', {'payment_order_id': order['payment_order_id']}, {
        'expires_at': datetime.now(timezone.utc) - timedelta(seconds=1)
    })
    with pytest.raises(ValueError, match='payment_order_expired'):
        svc.submit_tx_hash('u2', order['payment_order_id'], '0x' + 'c' * 64)
    assert db.find_one('payment_orders', {'payment_order_id': order['payment_order_id']})['status'] == 'EXPIRED'


def test_registration_can_attach_referral_and_generates_own_code():
    db = Database()
    seed(db, 'ref3', 'KAELEON-REF3')
    auth = AuthService(db)
    result = auth.create_registration('+15551234568', 'StrongPass123', '+1', 'KAELEON-REF3')
    user = db.find_one('users', {'user_id': result['user_id']})
    assert user['referral_code'].startswith('KAELEON-')
    assert user['referred_by_user_id'] == 'ref3'
