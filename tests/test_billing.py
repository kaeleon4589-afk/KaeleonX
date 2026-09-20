from datetime import datetime, timedelta, timezone
from app.billing import BillingService
from app.storage.database import Database


def seed_user(db):
    db.write('users', {'user_id':'u1','phone':'+15551234567','status':'active'})


def test_demo_unlimited_does_not_start_live_trial():
    db = Database()
    seed_user(db)
    svc = BillingService(db, trial_days=5)
    e = svc.entitlement('u1')
    assert e.demo_allowed is True
    assert e.live_allowed is False
    assert e.live_state == 'not_started'


def test_first_live_activation_starts_five_day_trial_only_once():
    db = Database(); seed_user(db)
    svc = BillingService(db, trial_days=5)
    start = datetime(2026,1,1,tzinfo=timezone.utc)
    e = svc.activate_live_first_time('u1', start)
    assert e.live_allowed is True
    assert e.live_expires_at == start + timedelta(days=5)
    assert svc.entitlement('u1', start + timedelta(days=4, hours=23)).live_allowed
    assert not svc.entitlement('u1', start + timedelta(days=5)).live_allowed
    try:
        svc.activate_live_first_time('u1', start + timedelta(days=6))
        assert False, 'trial should not restart'
    except ValueError as exc:
        assert str(exc) == 'live_trial_already_used'


def test_subscription_activates_live_without_affecting_demo():
    db = Database(); seed_user(db)
    svc = BillingService(db)
    order = svc.create_payment_order('u1','15D','0xreceiver')
    assert order['amount_usdt'] == '5'
    db.upsert('payment_orders', {'payment_order_id': order['payment_order_id']}, {'tx_hash':'0x'+'a'*64, 'status':'VERIFYING'})
    class V:
        def verify(self, order, tx_hash): return {'valid':True,'status':'CONFIRMED','block_number':123}
    e = svc.confirm_payment(order['payment_order_id'], '0x'+'a'*64, V())
    assert e.demo_allowed is True
    assert e.live_allowed is True
    assert e.live_state == 'subscribed'
