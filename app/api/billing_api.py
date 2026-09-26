from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, Field
from app.auth.service import AuthService
from app.billing import BillingService, BscUsdtVerifier, SUBSCRIPTIONS
from app.billing.service import PaymentVerificationPending
from app.config.settings import get_settings
from app.storage.database import Database

router = APIRouter(prefix="/billing", tags=["billing"])
_db = None
_service = None


def service():
    global _db, _service
    if _service is None:
        s = get_settings()
        _db = Database(s.mongodb_uri, s.mongodb_database)
        _service = BillingService(_db, trial_days=s.live_trial_days)
    return _service


def current_user(authorization: str | None):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "authentication_required")
    token = authorization.split(" ", 1)[1].strip()
    user = AuthService(service().db).authenticate_token(token)
    if not user:
        raise HTTPException(401, "invalid_or_expired_session")
    return user


class PaymentOrderRequest(BaseModel):
    plan_code: str = Field(pattern="^(15D|30D)$")


class TxRequest(BaseModel):
    payment_order_id: str
    tx_hash: str = Field(pattern=r"^(?:0x|0X)[0-9a-fA-F]{64}$")


def public_order(order: dict | None) -> dict | None:
    """Explicit JSON-safe billing contract; never expose Mongo/internal fields."""
    if not order:
        return None
    allowed = (
        "payment_order_id",
        "plan_code",
        "duration_days",
        "amount_usdt",
        "network",
        "destination_wallet",
        "status",
        "created_at",
        "expires_at",
        "tx_hash",
        "submitted_at",
        "verified_at",
        "confirmed_at",
        "verification_reason",
        "verified_block_number",
        "expired_at",
        "cancelled_at",
        "cancellation_reason",
        "reused_existing",
    )
    return {key: order.get(key) for key in allowed if key in order}


@router.get("/plans")
def plans():
    return {
        "plans": [
            {"code": k, "days": v["days"], "price_usdt": str(v["price_usdt"])}
            for k, v in SUBSCRIPTIONS.items()
        ]
    }


@router.get("/entitlement")
def entitlement(authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    e = service().entitlement(user["user_id"])
    return {
        "demo_allowed": e.demo_allowed,
        "live_allowed": e.live_allowed,
        "live_state": e.live_state,
        "live_expires_at": e.live_expires_at,
        "reason": e.reason,
    }


@router.post("/live/activate-trial")
def activate_trial(authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    try:
        e = service().activate_live_first_time(user["user_id"])
        return {
            "live_state": e.live_state,
            "live_allowed": e.live_allowed,
            "live_expires_at": e.live_expires_at,
        }
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.get("/orders")
def user_orders(authorization: str | None = Header(default=None), limit: int = 20):
    user = current_user(authorization)
    # Also cleans old duplicate active orders created by older frontend builds.
    service().reconcile_payment_orders(user["user_id"])
    rows = service().db.find_many(
        "payment_orders",
        {"user_id": user["user_id"]},
        limit=min(max(limit, 1), 100),
        sort_field="created_at",
    )
    return {"items": [public_order(row) for row in rows]}


@router.post("/orders")
def create_order(req: PaymentOrderRequest, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    s = get_settings()
    try:
        return public_order(
            service().create_payment_order(
                user["user_id"], req.plan_code, s.payment_wallet, s.payment_network
            )
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/orders/tx")
def submit_tx(req: TxRequest, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    try:
        return public_order(
            service().submit_tx_hash(user["user_id"], req.payment_order_id, req.tx_hash)
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/orders/verify")
def verify_payment(req: TxRequest, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    order = service().db.find_one(
        "payment_orders",
        {"payment_order_id": req.payment_order_id, "user_id": user["user_id"]},
    )
    if not order:
        raise HTTPException(400, "payment_order_or_tx_invalid")

    # submit_tx_hash normalizes the hash to lowercase.
    normalized = "0x" + req.tx_hash[2:].lower()
    if order.get("tx_hash") != normalized:
        raise HTTPException(400, "payment_order_or_tx_invalid")

    s = get_settings()
    verifier = BscUsdtVerifier(s.bsc_rpc_url, s.usdt_bsc_contract, s.usdt_decimals)
    try:
        e = service().confirm_payment(req.payment_order_id, normalized, verifier)
        return {
            "confirmed": True,
            "status": "CONFIRMED",
            "live_state": e.live_state,
            "live_expires_at": e.live_expires_at,
            "reason": None,
        }
    except PaymentVerificationPending as exc:
        # Not an application error: the blockchain/RPC may simply need another moment.
        return {
            "confirmed": False,
            "status": "VERIFYING",
            "live_state": service().entitlement(user["user_id"]).live_state,
            "live_expires_at": service().entitlement(user["user_id"]).live_expires_at,
            "reason": str(exc),
        }
    except ValueError as exc:
        raise HTTPException(400, str(exc))
