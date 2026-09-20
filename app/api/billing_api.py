from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, Field
from app.auth.service import token_hash
from app.billing import BillingService, BscUsdtVerifier, SUBSCRIPTIONS
from app.config.settings import get_settings
from app.storage.database import Database

router = APIRouter(prefix="/billing", tags=["billing"])
_db = None
_service = None


def service():
    global _db, _service
    if _service is None:
        s = get_settings(); _db = Database(s.mongodb_uri, s.mongodb_database); _service = BillingService(_db, trial_days=s.live_trial_days)
    return _service


def current_user(authorization: str | None):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "authentication_required")
    token = authorization.split(" ", 1)[1].strip()
    session = service().db.find_one("sessions", {"token_hash": token_hash(token), "revoked": False})
    if not session:
        raise HTTPException(401, "invalid_session")
    user = service().db.find_one("users", {"user_id": session["user_id"]})
    if not user or user.get("status") != "active":
        raise HTTPException(401, "invalid_session")
    return user


class PaymentOrderRequest(BaseModel):
    plan_code: str = Field(pattern="^(15D|30D)$")


class TxRequest(BaseModel):
    payment_order_id: str
    tx_hash: str = Field(min_length=20)


@router.get("/plans")
def plans():
    return {"plans": [{"code": k, "days": v["days"], "price_usdt": str(v["price_usdt"])} for k, v in SUBSCRIPTIONS.items()]}


@router.get("/entitlement")
def entitlement(authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    e = service().entitlement(user["user_id"])
    return {"demo_allowed": e.demo_allowed, "live_allowed": e.live_allowed, "live_state": e.live_state,
            "live_expires_at": e.live_expires_at, "reason": e.reason}


@router.post("/live/activate-trial")
def activate_trial(authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    try:
        e = service().activate_live_first_time(user["user_id"])
        return {"live_state": e.live_state, "live_allowed": e.live_allowed, "live_expires_at": e.live_expires_at}
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.post("/orders")
def create_order(req: PaymentOrderRequest, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    s = get_settings()
    try:
        return service().create_payment_order(user["user_id"], req.plan_code, s.payment_wallet, s.payment_network)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/orders/tx")
def submit_tx(req: TxRequest, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    try:
        return service().submit_tx_hash(user["user_id"], req.payment_order_id, req.tx_hash)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/orders/verify")
def verify_payment(req: TxRequest, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    order = service().db.find_one("payment_orders", {"payment_order_id": req.payment_order_id, "user_id": user["user_id"]})
    if not order or order.get("tx_hash") != req.tx_hash:
        raise HTTPException(400, "payment_order_or_tx_invalid")
    s = get_settings()
    verifier = BscUsdtVerifier(s.bsc_rpc_url, s.usdt_bsc_contract, s.usdt_decimals)
    try:
        e = service().confirm_payment(req.payment_order_id, req.tx_hash, verifier)
        return {"confirmed": True, "live_state": e.live_state, "live_expires_at": e.live_expires_at}
    except ValueError as exc:
        raise HTTPException(400, str(exc))
