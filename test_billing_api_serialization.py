from datetime import datetime, timezone

from app.api.billing_api import public_order


class FakeObjectId:
    """Intentionally not JSON serializable; mirrors the Mongo _id failure mode."""


def test_public_order_strips_mongo_and_internal_fields():
    now = datetime.now(timezone.utc)
    raw = {
        "_id": FakeObjectId(),
        "payment_order_id": "PAY-ABC",
        "payment_key": "internal-secret-key",
        "user_id": "user-private-id",
        "plan_code": "15D",
        "duration_days": 15,
        "amount_usdt": "5",
        "network": "BNB_SMART_CHAIN",
        "destination_wallet": "0xreceiver",
        "status": "VERIFYING",
        "created_at": now,
        "expires_at": now,
        "tx_hash": "0x" + "a" * 64,
    }

    result = public_order(raw)

    assert result is not None
    assert result["payment_order_id"] == "PAY-ABC"
    assert result["status"] == "VERIFYING"
    assert result["created_at"] == now
    assert "_id" not in result
    assert "payment_key" not in result
    assert "user_id" not in result


def test_public_order_keeps_verification_status_fields():
    result = public_order({
        "payment_order_id": "PAY-XYZ",
        "status": "INVALID",
        "verification_reason": "tx_not_confirmed",
        "verified_block_number": None,
    })

    assert result == {
        "payment_order_id": "PAY-XYZ",
        "status": "INVALID",
        "verification_reason": "tx_not_confirmed",
        "verified_block_number": None,
    }
