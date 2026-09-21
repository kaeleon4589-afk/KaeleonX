from datetime import datetime, timedelta, timezone

from app.admin.access import grant_live_days
from app.auth.service import AuthService, token_hash
from app.billing.service import BillingService
from app.storage.database import Database
from app.trading.metrics import calculate_performance


class AuditStub:
    def event(self, *args, **kwargs):
        return None


class ValidVerifier:
    def __init__(self):
        self.calls = 0

    def verify(self, order, tx_hash):
        self.calls += 1
        return {"valid": True, "status": "CONFIRMED", "block_number": 123}


def seed_user(db, user_id="u1"):
    db.write("users", {"user_id": user_id, "phone": "+15550000001", "status": "active"})


def test_confirm_payment_is_idempotent_and_does_not_add_days_twice():
    db = Database()
    seed_user(db)
    billing = BillingService(db)
    order = billing.create_payment_order("u1", "15D", "0xreceiver")
    tx = "0x" + "a" * 64
    db.upsert("payment_orders", {"payment_order_id": order["payment_order_id"]}, {"tx_hash": tx, "status": "VERIFYING"})
    verifier = ValidVerifier()

    first = billing.confirm_payment(order["payment_order_id"], tx, verifier)
    first_expiry = first.live_expires_at
    second = billing.confirm_payment(order["payment_order_id"], tx, verifier)

    assert verifier.calls == 1
    assert second.live_expires_at == first_expiry
    assert db.count("events", {"event_type": "SUBSCRIPTION_ACTIVATED"}) == 1


def test_admin_live_days_are_recognized_by_billing_and_extend_existing_access():
    db = Database()
    seed_user(db)
    now = datetime.now(timezone.utc)
    db.upsert("users", {"user_id": "u1"}, {"subscription_expires_at": now + timedelta(days=5), "live_state": "subscribed"})
    user = db.find_one("users", {"user_id": "u1"})

    granted_until = grant_live_days(db, user, 7, "admin", AuditStub())
    entitlement = BillingService(db).entitlement("u1", now)

    assert granted_until >= now + timedelta(days=11, hours=23)
    assert entitlement.live_allowed is True
    assert entitlement.live_state == "granted"
    assert entitlement.live_expires_at == granted_until


def test_expired_session_is_rejected_and_blocked_user_session_is_rejected():
    db = Database()
    seed_user(db)
    auth = AuthService(db)
    token = "test-token"
    db.write("sessions", {
        "token_hash": token_hash(token),
        "user_id": "u1",
        "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
        "revoked": False,
    })
    assert auth.authenticate_token(token) is None

    active_token = "active-token"
    db.write("sessions", {
        "token_hash": token_hash(active_token),
        "user_id": "u1",
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "revoked": False,
    })
    db.upsert("users", {"user_id": "u1"}, {"status": "blocked"})
    assert auth.get_current_user(active_token) is None


def test_performance_metrics_are_calculated_from_closed_positions():
    positions = [
        {"position_id": "p1", "status": "CLOSED", "closed_at": 1, "realized_pnl": 5, "entry_fee": 0.5, "exit_fee": 0.5},
        {"position_id": "p2", "status": "CLOSED", "closed_at": 2, "realized_pnl": -2, "entry_fee": 0, "exit_fee": 0},
        {"position_id": "p3", "status": "OPEN", "realized_pnl": 100},
    ]
    result = calculate_performance(positions, 100)

    assert result["trades"] == 2
    assert result["pnl"] == 2
    assert result["current_capital"] == 102
    assert result["win_rate"] == 50
    assert result["profit_factor"] == 2
    assert result["drawdown"] > 0
