from datetime import datetime, timedelta, timezone

import pytest

from app.billing.service import BillingService, BscUsdtVerifier, PaymentVerificationPending
from app.storage.database import Database


def seed_user(db, user_id='u1'):
    db.write('users', {'user_id': user_id, 'phone': '+15551234567', 'status': 'active'})


def test_create_payment_order_is_idempotent_while_one_is_active():
    db = Database(); seed_user(db)
    svc = BillingService(db)
    first = svc.create_payment_order('u1', '15D', '0x' + '1' * 40)
    second = svc.create_payment_order('u1', '30D', '0x' + '1' * 40)

    assert second['payment_order_id'] == first['payment_order_id']
    assert second['duration_days'] == 15
    assert second['reused_existing'] is True
    rows = db.find_many('payment_orders', {'user_id': 'u1'}, limit=0)
    assert len(rows) == 1


def test_reconcile_keeps_awaiting_order_active_and_releases_invalid_order_slot():
    db = Database(); seed_user(db)
    svc = BillingService(db)
    now = datetime.now(timezone.utc)
    db.write('payment_orders', {
        'payment_order_id': 'PAY-A', 'user_id': 'u1', 'plan_code': '15D', 'duration_days': 15,
        'amount_usdt': '5', 'network': 'BNB_SMART_CHAIN', 'destination_wallet': '0x' + '1' * 40,
        'status': 'AWAITING_PAYMENT', 'created_at': now, 'expires_at': now + timedelta(minutes=30), 'tx_hash': None,
        'active_order_key': 'u1',
    })
    db.write('payment_orders', {
        'payment_order_id': 'PAY-B', 'user_id': 'u1', 'plan_code': '15D', 'duration_days': 15,
        'amount_usdt': '5', 'network': 'BNB_SMART_CHAIN', 'destination_wallet': '0x' + '1' * 40,
        'status': 'INVALID', 'created_at': now + timedelta(seconds=1), 'expires_at': now + timedelta(minutes=30),
        'tx_hash': '0x' + 'a' * 64, 'submitted_at': now + timedelta(seconds=2),
        'active_order_key': 'u1',
    })

    active = svc.reconcile_payment_orders('u1', now + timedelta(seconds=3))
    assert active['payment_order_id'] == 'PAY-A'
    invalid = db.find_one('payment_orders', {'payment_order_id': 'PAY-B'})
    assert invalid['status'] == 'INVALID'
    assert invalid.get('active_order_key') is None


def test_tx_hash_must_be_full_32_byte_evm_hash():
    db = Database(); seed_user(db)
    svc = BillingService(db)
    order = svc.create_payment_order('u1', '15D', '0x' + '1' * 40)
    with pytest.raises(ValueError, match='invalid_tx_hash'):
        svc.submit_tx_hash('u1', order['payment_order_id'], '0x' + 'a' * 32)

    accepted = svc.submit_tx_hash('u1', order['payment_order_id'], '0X' + 'A' * 64)
    assert accepted['tx_hash'] == '0x' + 'a' * 64
    assert accepted['status'] == 'VERIFYING'


def test_pending_blockchain_confirmation_does_not_mark_order_invalid():
    db = Database(); seed_user(db)
    svc = BillingService(db)
    order = svc.create_payment_order('u1', '15D', '0x' + '1' * 40)
    tx = '0x' + 'a' * 64
    svc.submit_tx_hash('u1', order['payment_order_id'], tx)

    class PendingVerifier:
        def verify(self, order, tx_hash):
            return {'valid': False, 'status': 'VERIFYING', 'reason': 'tx_not_confirmed'}

    with pytest.raises(PaymentVerificationPending, match='tx_not_confirmed'):
        svc.confirm_payment(order['payment_order_id'], tx, PendingVerifier())

    stored = db.find_one('payment_orders', {'payment_order_id': order['payment_order_id']})
    assert stored['status'] == 'VERIFYING'
    assert stored['verification_reason'] == 'tx_not_confirmed'


def test_bsc_verifier_distinguishes_pending_reverted_and_valid_transfer():
    tx = '0x' + 'b' * 64
    receiver = '0x' + '2' * 40
    contract = '0x55d398326f99059fF775485246999027B3197955'
    order = {'destination_wallet': receiver, 'amount_usdt': '5'}
    verifier = BscUsdtVerifier('https://example.invalid', contract, 18)

    def pending_rpc(method, params):
        if method == 'eth_chainId': return '0x38'
        return None
    verifier._rpc = pending_rpc
    assert verifier.verify(order, tx)['status'] == 'VERIFYING'

    def reverted_rpc(method, params):
        if method == 'eth_chainId': return '0x38'
        return {'status': '0x0', 'logs': []}
    verifier._rpc = reverted_rpc
    reverted = verifier.verify(order, tx)
    assert reverted['status'] == 'INVALID'
    assert reverted['reason'] == 'tx_reverted'

    to_topic = '0x' + ('0' * 24) + receiver[2:].lower()
    amount_hex = hex(5 * 10**18)
    def valid_rpc(method, params):
        if method == 'eth_chainId': return '0x38'
        return {
            'status': '0x1', 'blockNumber': '0x10',
            'logs': [{
                'address': contract,
                'topics': [BscUsdtVerifier.TRANSFER_TOPIC, '0x' + '0' * 64, to_topic],
                'data': amount_hex,
            }],
        }
    verifier._rpc = valid_rpc
    valid = verifier.verify(order, tx)
    assert valid['valid'] is True
    assert valid['block_number'] == 16


def test_hash_submitted_before_expiry_can_confirm_after_expiry():
    db = Database(); seed_user(db)
    svc = BillingService(db)
    order = svc.create_payment_order('u1', '15D', '0x' + '1' * 40)
    tx = '0x' + 'c' * 64
    past = datetime.now(timezone.utc) - timedelta(minutes=2)
    submitted = past - timedelta(minutes=1)
    db.upsert('payment_orders', {'payment_order_id': order['payment_order_id']}, {
        'expires_at': past,
        'submitted_at': submitted,
        'tx_hash': tx,
        'status': 'VERIFYING',
    })

    class ValidVerifier:
        def verify(self, order, tx_hash):
            return {'valid': True, 'status': 'CONFIRMED', 'block_number': 99}

    entitlement = svc.confirm_payment(order['payment_order_id'], tx, ValidVerifier())
    assert entitlement.live_allowed is True
    assert db.find_one('payment_orders', {'payment_order_id': order['payment_order_id']})['status'] == 'CONFIRMED'


def test_bsc_verifier_rejects_transfer_older_than_payment_order():
    tx = '0x' + 'd' * 64
    receiver = '0x' + '3' * 40
    contract = '0x55d398326f99059fF775485246999027B3197955'
    created_at = datetime.now(timezone.utc)
    order = {'destination_wallet': receiver, 'amount_usdt': '5', 'created_at': created_at}
    verifier = BscUsdtVerifier('https://example.invalid', contract, 18)
    old_ts = int((created_at - timedelta(minutes=10)).timestamp())

    def rpc(method, params):
        if method == 'eth_chainId': return '0x38'
        if method == 'eth_getTransactionReceipt':
            return {'status': '0x1', 'blockNumber': '0x20', 'logs': []}
        if method == 'eth_getBlockByNumber':
            return {'timestamp': hex(old_ts)}
        raise AssertionError(method)

    verifier._rpc = rpc
    result = verifier.verify(order, tx)
    assert result['valid'] is False
    assert result['reason'] == 'tx_before_order'


def test_invalid_order_with_hash_keeps_reverify_grace_after_expiry():
    db = Database(); seed_user(db)
    svc = BillingService(db)
    order = svc.create_payment_order('u1', '15D', '0x' + '1' * 40)
    now = datetime.now(timezone.utc)
    expired = now - timedelta(minutes=10)
    db.upsert('payment_orders', {'payment_order_id': order['payment_order_id']}, {
        'expires_at': expired,
        'submitted_at': expired - timedelta(minutes=1),
        'status': 'INVALID',
        'tx_hash': '0x' + 'a' * 64,
    })
    corrected = '0x' + 'b' * 64
    updated = svc.submit_tx_hash('u1', order['payment_order_id'], corrected)
    assert updated['status'] == 'VERIFYING'
    assert updated['tx_hash'] == corrected


def test_invalid_order_releases_slot_and_new_order_is_created():
    db = Database()
    user_id = "user-invalid-new-order"
    db.write("users", {"user_id": user_id, "phone": "+15550000001"})
    billing = BillingService(db)

    first = billing.create_payment_order(user_id, "15D", "0xreceiver")
    db.upsert("payment_orders", {"payment_order_id": first["payment_order_id"]}, {
        "status": "INVALID",
        "verification_reason": "amount_or_destination_mismatch",
        "active_order_key": user_id,
        "tx_hash": "0x" + "a" * 64,
    })

    second = billing.create_payment_order(user_id, "15D", "0xreceiver")
    assert second["payment_order_id"] != first["payment_order_id"]
    assert second["status"] == "AWAITING_PAYMENT"
    assert not second.get("reused_existing", False)

    old = db.find_one("payment_orders", {"payment_order_id": first["payment_order_id"]})
    assert old["status"] == "INVALID"
    assert old.get("active_order_key") is None


def test_verifying_order_still_blocks_duplicate_creation():
    db = Database()
    user_id = "user-verifying-reuse"
    db.write("users", {"user_id": user_id, "phone": "+15550000002"})
    billing = BillingService(db)

    first = billing.create_payment_order(user_id, "15D", "0xreceiver")
    db.upsert("payment_orders", {"payment_order_id": first["payment_order_id"]}, {
        "status": "VERIFYING",
        "active_order_key": user_id,
        "tx_hash": "0x" + "b" * 64,
        "submitted_at": datetime.now(timezone.utc),
    })

    second = billing.create_payment_order(user_id, "15D", "0xreceiver")
    assert second["payment_order_id"] == first["payment_order_id"]
    assert second.get("reused_existing") is True
