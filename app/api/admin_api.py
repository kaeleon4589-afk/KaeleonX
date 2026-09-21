from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from app.auth.service import AuthService
from app.config.settings import get_settings
from app.logging.logger import AuditLogger
from app.storage.database import Database
from app.auth.service import normalize_phone
from app.admin.access import find_user, grant_live_days, set_user_status

router = APIRouter(prefix="/admin", tags=["admin"])
_db = None
_auth = None
_audit = None

def deps():
    global _db, _auth, _audit
    if _auth is None:
        s = get_settings()
        _db = Database(s.mongodb_uri, s.mongodb_database)
        _auth = AuthService(_db)
        _audit = AuditLogger(_db)
    return _db, _auth, _audit

def current_admin(authorization: str | None):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "authentication_required")
    token = authorization.split(" ", 1)[1].strip()
    db, auth, audit = deps()
    user = auth.authenticate_token(token)
    if not user:
        raise HTTPException(401, "invalid_or_expired_session")
    configured = get_settings().admin_phone
    try:
        configured = normalize_phone(configured) if configured else ''
    except ValueError:
        configured = ''
    if not configured or normalize_phone(user.get('phone', '')) != configured:
        audit.event("ADMIN_ACCESS_DENIED", user_id=user.get("user_id"), phone=user.get("phone"))
        raise HTTPException(403, "admin_access_denied")
    audit.event("ADMIN_ACCESS_GRANTED", user_id=user.get("user_id"))
    return user



class UserLookup(BaseModel):
    phone: str | None = None
    telegram_user_id: str | None = None

class GrantLiveDaysRequest(UserLookup):
    days: int = Field(gt=0, le=3650)

class BanUserRequest(UserLookup):
    days: int | None = Field(default=None, gt=0, le=3650)
    reason: str | None = Field(default=None, max_length=500)

def target_user(req):
    db=deps()[0]
    if req.phone: return find_user(db, phone=normalize_phone(req.phone))
    if req.telegram_user_id: return find_user(db, telegram_user_id=req.telegram_user_id)
    raise HTTPException(400, 'phone_or_telegram_user_id_required')

def require_target(req):
    user=target_user(req)
    if not user: raise HTTPException(404, 'user_not_found')
    return user

def clean(doc):
    if not doc:
        return None
    out = dict(doc)
    for key in ('_id', 'password_hash'):
        out.pop(key, None)
    return out

@router.get("/me")
def admin_me(authorization: str | None = Header(default=None)):
    return clean(current_admin(authorization))

@router.get("/dashboard")
def dashboard(authorization: str | None = Header(default=None)):
    current_admin(authorization)
    db, _, _ = deps()
    return {
        "users": {
            "total": db.count("users"),
            "active": db.count("users", {"status": "active"}),
            "blocked": db.count("users", {"status": "blocked"}),
            "suspended": db.count("users", {"status": "suspended"}),
        },
        "trading": {
            "open_positions": db.count("positions", {"status": "OPEN"}),
            "orders": db.count("orders"),
            "decisions": db.count("decisions"),
        },
        "payments": {
            "total": db.count("payment_orders"),
            "pending": db.count("payment_orders", {"status": {"$in": ["PENDING", "AWAITING_PAYMENT", "VERIFYING"]}}),
            "confirmed": db.count("payment_orders", {"status": "CONFIRMED"}),
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

@router.get("/users")
def users(authorization: str | None = Header(default=None), limit: int = 100):
    current_admin(authorization)
    db, _, _ = deps()
    return {"items": [clean(x) for x in db.find_many("users", limit=min(max(limit, 1), 200), sort_field="created_at")]}

@router.get("/payments")
def payments(authorization: str | None = Header(default=None), limit: int = 100):
    current_admin(authorization)
    db, _, _ = deps()
    return {"items": [clean(x) for x in db.find_many("payment_orders", limit=min(max(limit, 1), 200), sort_field="created_at")]}

@router.get("/trading/positions")
def trading_positions(authorization: str | None = Header(default=None), limit: int = 100):
    current_admin(authorization)
    db, _, _ = deps()
    return {"items": [clean(x) for x in db.find_many("positions", limit=min(max(limit, 1), 200), sort_field="updated_at")]}

@router.get("/trading/decisions")
def trading_decisions(authorization: str | None = Header(default=None), limit: int = 100):
    current_admin(authorization)
    db, _, _ = deps()
    return {"items": [clean(x) for x in db.find_many("decisions", limit=min(max(limit, 1), 200), sort_field="updated_at")]}

@router.post("/users/live-days")
def grant_live_days_admin(req: GrantLiveDaysRequest, authorization: str | None = Header(default=None)):
    admin = current_admin(authorization)
    db, _, audit = deps()
    user = require_target(req)
    until = grant_live_days(db, user, req.days, admin["user_id"], audit)
    updated = db.find_one("users", {"user_id": user["user_id"]})
    return {"success": True, "user": clean(updated), "live_access_until": until.isoformat(), "days_added": req.days}

@router.post("/users/ban")
def ban_user(req: BanUserRequest, authorization: str | None = Header(default=None)):
    admin = current_admin(authorization)
    db, _, audit = deps()
    user = require_target(req)
    until = None
    if req.days:
        until = datetime.now(timezone.utc) + timedelta(days=req.days)
    set_user_status(db, user, "suspended" if until else "blocked", admin["user_id"], audit, req.reason, until)
    updated = db.find_one("users", {"user_id": user["user_id"]})
    return {"success": True, "user": clean(updated), "ban_until": until.isoformat() if until else None}

@router.post("/users/unban")
def unban_user(req: UserLookup, authorization: str | None = Header(default=None)):
    admin = current_admin(authorization)
    db, _, audit = deps()
    user = require_target(req)
    set_user_status(db, user, "active", admin["user_id"], audit, "ADMIN_UNBAN")
    updated = db.find_one("users", {"user_id": user["user_id"]})
    return {"success": True, "user": clean(updated)}

@router.delete("/users")
def delete_user(req: UserLookup, authorization: str | None = Header(default=None)):
    admin = current_admin(authorization)
    db, _, audit = deps()
    user = require_target(req)
    # Logical deletion: blocks access while retaining records for audit and allowing admin restoration.
    set_user_status(db, user, "deleted", admin["user_id"], audit, "ADMIN_DELETE")
    updated = db.find_one("users", {"user_id": user["user_id"]})
    return {"success": True, "reversible": True, "user": clean(updated)}

@router.post("/users/restore")
def restore_user(req: UserLookup, authorization: str | None = Header(default=None)):
    admin = current_admin(authorization)
    db, _, audit = deps()
    user = require_target(req)
    set_user_status(db, user, "active", admin["user_id"], audit, "ADMIN_RESTORE")
    updated = db.find_one("users", {"user_id": user["user_id"]})
    return {"success": True, "user": clean(updated)}

@router.get("/system/events")
def system_events(authorization: str | None = Header(default=None), limit: int = 200):
    current_admin(authorization)
    db, _, _ = deps()
    return {"items": [clean(x) for x in db.find_many("events", limit=min(max(limit, 1), 500), sort_field="created_at")]}

@router.get("/system/config")
def system_config(authorization: str | None = Header(default=None)):
    current_admin(authorization)
    s = get_settings()
    return {
        "environment": s.environment,
        "mode": "MULTI_USER",
        "default_symbol": s.default_symbol,
        "default_timeframe": s.default_timeframe,
        "fixed_leverage": s.fixed_leverage,
        "risk_per_trade": s.risk_per_trade,
        "coinw_rest_base_url": s.coinw_rest_base_url,
        "coinw_ws_url": s.coinw_ws_url,
        "admin_phone_configured": bool(s.admin_phone),
    }

@router.get('/referrals')
def admin_referrals(authorization: str | None = Header(default=None), limit: int = 200):
    current_admin(authorization)
    db, _, _ = deps()
    users = db.find_many('users', limit=min(max(limit, 1), 500), sort_field='created_at')
    by_id = {str(u.get('user_id')): u for u in users}
    referred = [u for u in users if u.get('referred_by_user_id')]
    items = []
    for row in referred:
        referrer = by_id.get(str(row.get('referred_by_user_id'))) or db.find_one('users', {'user_id': row.get('referred_by_user_id')}) or {}
        items.append({
            'referred_user_id': row.get('user_id'),
            'referred_phone': row.get('phone'),
            'referrer_user_id': row.get('referred_by_user_id'),
            'referrer_phone': referrer.get('phone'),
            'code': row.get('referred_by_code'),
            'created_at': row.get('referral_attached_at') or row.get('created_at'),
            'rewarded': bool(row.get('referral_rewarded_at')),
            'reward_days': int(row.get('referral_reward_days', 0) or 0),
        })
    return {
        'items': items[:min(max(limit, 1), 500)],
        'total': len(items),
        'rewarded': sum(1 for item in items if item['rewarded']),
    }


@router.get('/subscriptions')
def admin_subscriptions(authorization: str | None = Header(default=None), limit: int = 200):
    current_admin(authorization)
    db, _, _ = deps()
    rows = db.find_many('users', limit=min(max(limit, 1), 500), sort_field='created_at')
    items = []
    for row in rows:
        if row.get('live_state') not in ('not_started', None) or row.get('subscription_expires_at') or row.get('live_access_until'):
            items.append({
                'user_id': row.get('user_id'),
                'phone': row.get('phone'),
                'live_state': row.get('live_state'),
                'subscription_expires_at': row.get('subscription_expires_at'),
                'live_access_until': row.get('live_access_until'),
                'referral_reward_days_total': int(row.get('referral_reward_days_total', 0) or 0),
            })
    return {'items': items[:min(max(limit, 1), 500)]}


@router.get('/operations')
def admin_operations(authorization: str | None = Header(default=None), limit: int = 200):
    current_admin(authorization)
    db, _, _ = deps()
    return {'items': [clean(x) for x in db.find_many('positions', limit=min(max(limit, 1), 500), sort_field='created_at')]}
