from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.config.settings import get_settings
from app.auth.service import AuthService
from app.billing.service import BillingService
from app.coinw.account import CoinWAccountAPI
from app.coinw.rest_client import CoinWRestClient
from app.security.credential_vault import CredentialVault
from app.storage.database import Database
from app.trading.profile import UserTradingProfileService
from app.trading.metrics import calculate_performance
from app.referrals import new_referral_code

router = APIRouter(prefix="/user", tags=["user"])
_db = None
_auth = None
_profiles = None
_billing = None


def deps():
    global _db, _auth, _profiles, _billing
    s = get_settings()
    if _db is None:
        _db = Database(s.mongodb_uri, s.mongodb_database)
    if _auth is None:
        _auth = AuthService(_db)
    if _profiles is None:
        try:
            vault = CredentialVault(s.credential_encryption_key)
        except ValueError as exc:
            raise HTTPException(503, str(exc)) from exc
        _profiles = UserTradingProfileService(
            _db,
            vault,
            minimum_operating_capital=s.min_operating_capital,
        )
    if _billing is None:
        _billing = BillingService(_db, trial_days=s.live_trial_days)
    return _db, _auth, _profiles, _billing


def current(authorization):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "authentication_required")
    _, token = authorization.split(" ", 1)
    db, auth, profiles, billing = deps()
    user = auth.get_current_user(token.strip())
    if not user:
        raise HTTPException(401, "invalid_or_expired_session")
    return db, user, profiles, billing


def collection(db, name, query):
    if db.db is not None:
        return list(db.db[name].find(query, {"_id": 0}).sort("created_at", -1).limit(50))
    rows = [d for c, d in db.memory if c == name and all(d.get(k) == v for k, v in query.items())]
    return list(reversed(rows[-50:]))


def _extract_available_equity(response) -> float:
    data = response.get("data", response) if isinstance(response, dict) else response
    if isinstance(data, dict):
        for key in ("availableUsdt", "availableMargin", "available", "balance"):
            value = data.get(key)
            if value is not None:
                try:
                    return max(0.0, float(value))
                except (TypeError, ValueError):
                    pass
    return 0.0


async def _coinw_equity(profiles: UserTradingProfileService, user_id: str) -> float:
    settings = get_settings()
    creds = profiles.credentials(user_id)
    client = CoinWRestClient(settings.coinw_rest_base_url, creds.api_key, creds.api_secret)
    account = CoinWAccountAPI(client)
    response = await account.assets("usdt")
    return _extract_available_equity(response)


class TradingConfigRequest(BaseModel):
    execution_mode: Literal["demo", "live"] = "demo"
    trading_enabled: bool = False
    operating_capital: float | None = Field(default=None, gt=0)
    coinw_api_key: str | None = Field(default=None, min_length=1)
    coinw_api_secret: str | None = Field(default=None, min_length=1)


def _config_payload(profile, entitlement, settings):
    active_available = settings.paper_initial_equity if profile.execution_mode == "demo" else profile.coinw_available_equity
    return {
        "execution_mode": profile.execution_mode,
        "trading_enabled": profile.trading_enabled,
        "operating_capital": profile.operating_capital,
        "demo_operating_capital": profile.demo_operating_capital,
        "live_operating_capital": profile.live_operating_capital,
        "minimum_operating_capital": settings.min_operating_capital,
        "demo_available_equity": float(settings.paper_initial_equity),
        "live_available_equity": profile.coinw_available_equity,
        "available_equity": float(active_available),
        "coinw_configured": profile.coinw_configured,
        "coinw_verified": profile.coinw_verified,
        "coinw_verified_at": profile.coinw_verified_at,
        "coinw_api_key": profile.coinw_api_key_masked,
        "live_allowed": entitlement.live_allowed,
        "live_state": entitlement.live_state,
    }


@router.get("/trading-config")
def trading_config(authorization: str | None = Header(default=None)):
    _, user, profiles, billing = current(authorization)
    profile = profiles.public(user["user_id"])
    return _config_payload(profile, billing.entitlement(user["user_id"]), get_settings())


@router.put("/trading-config")
async def save_trading_config(req: TradingConfigRequest, authorization: str | None = Header(default=None)):
    _, user, profiles, billing = current(authorization)
    uid = user["user_id"]
    settings = get_settings()
    if req.execution_mode == "live" and not billing.entitlement(uid).live_allowed:
        raise HTTPException(403, "live_not_entitled")

    try:
        # Validate limits before persisting capital so a rejected request cannot
        # leave a too-large allocation stored in the profile.
        if req.operating_capital is not None:
            requested = float(req.operating_capital)
            before = profiles.public(uid)
            if not before.coinw_verified:
                raise ValueError("coinw_verification_required_for_capital")
            if req.execution_mode == "demo":
                if requested > settings.paper_initial_equity:
                    raise ValueError("demo_capital_exceeds_virtual_balance")
            else:
                available = await _coinw_equity(profiles, uid)
                profiles.update_available_equity(uid, available)
                if requested > available:
                    raise ValueError("live_capital_exceeds_available_balance")

        # Credentials are saved intentionally as unverified. The user must
        # explicitly test them before capital/trading can be enabled.
        row = profiles.save(
            uid,
            execution_mode=req.execution_mode,
            trading_enabled=req.trading_enabled,
            operating_capital=req.operating_capital,
            api_key=req.coinw_api_key,
            api_secret=req.coinw_api_secret,
        )
        public = profiles.public(uid)
        return {
            "saved": True,
            **_config_payload(public, billing.entitlement(uid), settings),
            "profile_updated_at": row.get("updated_at"),
        }
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(400, f"coinw_balance_check_failed:{exc}") from exc


@router.delete("/coinw-credentials")
def delete_coinw_credentials(authorization: str | None = Header(default=None)):
    _, user, profiles, _ = current(authorization)
    try:
        profiles.clear_credentials(user["user_id"])
        return {"deleted": True, "coinw_configured": False, "coinw_verified": False}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/coinw/test")
async def test_coinw_credentials(authorization: str | None = Header(default=None)):
    _, user, profiles, _ = current(authorization)
    try:
        available = await _coinw_equity(profiles, user["user_id"])
        profiles.mark_verified(user["user_id"], available)
        return {
            "connected": True,
            "verified": True,
            "available_equity": available,
            "demo_equity": float(get_settings().paper_initial_equity),
            "api_key": profiles.public(user["user_id"]).coinw_api_key_masked,
        }
    except Exception as exc:
        raise HTTPException(400, f"coinw_connection_failed:{exc}") from exc


@router.get("/dashboard")
def dashboard(authorization: str | None = Header(default=None)):
    db, user, profiles, _ = current(authorization)
    uid = user["user_id"]
    profile = profiles.public(uid)
    mode = profile.execution_mode
    state = db.find_one("user_engine_state", {"user_id": uid, "mode": mode.upper()}) or {
        "mode": mode.upper(), "status": "PAUSADO", "capital": 0.0,
        "pnl": 0.0, "pnl_pct": 0.0, "regime": "TRANSITION",
        "direction": "NEUTRAL", "strategy": None, "open_position": None,
    }
    state["configured_capital"] = profile.operating_capital
    state["trading_enabled"] = profile.trading_enabled
    return {"user": {"user_id": uid, "role": user.get("role")}, "engine": state}


@router.get("/execution")
def execution(authorization: str | None = Header(default=None)):
    db, user, profiles, _ = current(authorization)
    profile = profiles.public(user["user_id"])
    mode = profile.execution_mode
    state = db.find_one("user_engine_state", {"user_id": user["user_id"], "mode": mode.upper()}) or {}
    return {
        "mode": state.get("mode", mode.upper()),
        "status": state.get("status", "PAUSADO" if not profile.trading_enabled else "ANALIZANDO"),
        "capital": state.get("capital", profile.operating_capital),
        "configured_capital": profile.operating_capital,
        "available_equity": state.get(
            "available_equity",
            get_settings().paper_initial_equity if mode == "demo" else profile.coinw_available_equity,
        ),
        "markets_scanned": state.get("markets_scanned", 0),
        "candidates": state.get("candidates", 0),
        "coinw_connected": profile.coinw_verified,
        "leverage": "INTERNAL",
        "market_selection": "KAELEON_AUTO",
        "timeframe_selection": "KAELEON_INTERNAL",
        "trading_enabled": profile.trading_enabled,
    }


@router.get("/operations")
def operations(authorization: str | None = Header(default=None)):
    db, user, profiles, _ = current(authorization)
    uid = user["user_id"]
    mode = profiles.public(uid).execution_mode
    all_positions = collection(db, "positions", {"user_id": uid, "mode": mode})
    return {
        "mode": mode,
        "open": [p for p in all_positions if str(p.get("status", "OPEN")).upper() == "OPEN"],
        "closed": [p for p in all_positions if str(p.get("status", "OPEN")).upper() != "OPEN"],
    }


@router.get("/performance")
def performance(authorization: str | None = Header(default=None)):
    db, user, profiles, _ = current(authorization)
    uid = user["user_id"]
    profile = profiles.public(uid)
    mode = profile.execution_mode
    state = db.find_one("user_engine_state", {"user_id": uid, "mode": mode.upper()}) or {}
    capital = float(profile.operating_capital)
    positions = db.find_many("positions", {"user_id": uid, "mode": mode}, limit=10000)
    metrics = calculate_performance(positions, capital)
    default_available = get_settings().paper_initial_equity if mode == "demo" else profile.coinw_available_equity
    return {
        "mode": mode,
        "capital": capital,
        "configured_capital": capital,
        **metrics,
        "available_equity": float(state.get("available_equity", default_available)),
    }


@router.get("/decision/{decision_id}")
def decision(decision_id: str, authorization: str | None = Header(default=None)):
    db, user, _, _ = current(authorization)
    d = db.find_one("decisions", {"decision_id": decision_id, "user_id": user["user_id"]})
    if not d:
        raise HTTPException(404, "decision_not_found")
    d.pop("_id", None)
    return d


@router.get("/settings")
def settings(authorization: str | None = Header(default=None)):
    _, user, profiles, _ = current(authorization)
    profile = profiles.public(user["user_id"])
    return {
        "phone": user.get("phone"),
        "role": user.get("role"),
        "tutorial_completed": bool(user.get("tutorial_completed", False)),
        "market_selection": "AUTOMATIC_BY_KAELEON",
        "timeframes": "INTERNAL_MTF_BY_STRATEGY",
        "leverage": "FIXED_INTERNAL",
        "execution_mode": profile.execution_mode,
        "trading_enabled": profile.trading_enabled,
        "operating_capital": profile.operating_capital,
        "coinw_configured": profile.coinw_configured,
        "coinw_verified": profile.coinw_verified,
        "coinw_api_key": profile.coinw_api_key_masked,
    }


def _mask_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    value = str(phone)
    if len(value) <= 5:
        return value
    return f"{value[:3]}{'*' * max(2, len(value) - 5)}{value[-2:]}"


@router.get('/referrals')
def referrals(authorization: str | None = Header(default=None)):
    db, user, _, _ = current(authorization)
    uid = user['user_id']
    fresh = db.find_one('users', {'user_id': uid}) or user
    if not fresh.get('referral_code'):
        code = new_referral_code(db)
        db.upsert('users', {'user_id': uid}, {'referral_code': code})
        fresh = {**fresh, 'referral_code': code}
    referred = db.find_many('users', {'referred_by_user_id': uid}, limit=200, sort_field='created_at')
    reward_rows = db.find_many('referral_rewards', {'referrer_user_id': uid}, limit=500, sort_field='created_at')
    reward_by_referred = {str(row.get('referred_user_id')): row for row in reward_rows}
    items = []
    for row in referred:
        reward = reward_by_referred.get(str(row.get('user_id')))
        items.append({
            'user_id': row.get('user_id'),
            'phone_masked': _mask_phone(row.get('phone')),
            'status': row.get('status'),
            'created_at': row.get('created_at'),
            'rewarded': bool(row.get('referral_rewarded_at') or reward),
            'reward_days': int((reward or {}).get('reward_days') or row.get('referral_reward_days') or 0),
            'rewarded_at': row.get('referral_rewarded_at') or (reward or {}).get('created_at'),
        })
    return {
        'referral_code': fresh.get('referral_code'),
        'referred_count': len(referred),
        'rewarded_count': sum(1 for item in items if item['rewarded']),
        'reward_days_total': int(fresh.get('referral_reward_days_total', 0) or 0),
        'reward_rules': {'15': 7, '30': 15},
        'items': items,
    }


@router.get("/activity")
def activity(
    authorization: str | None = Header(default=None),
    limit: int = Query(default=25, ge=1, le=50),
    mode: str | None = Query(default=None),
):
    """Lightweight monitor data without storing operational logs in MongoDB.

    The current regime/strategy comes from the upserted engine state and recent
    trade activity comes from the positions collection, which is business data
    we need anyway. No append-only events collection is required.
    """
    db, user, profiles, _ = current(authorization)
    uid = user["user_id"]
    active_mode = (mode or profiles.public(uid).execution_mode).lower()
    state = db.find_one("user_engine_state", {"user_id": uid, "mode": active_mode.upper()}) or {}
    rows = db.find_many("positions", {"user_id": uid, "mode": active_mode}, limit=max(limit * 2, 20), sort_field="opened_at", descending=True)

    items = []
    if state.get("regime") or state.get("regime_candidate"):
        items.append({
            "event": "REGIME_STATE",
            "mode": active_mode,
            "symbol": state.get("last_symbol"),
            "regime": state.get("regime"),
            "candidate": state.get("regime_candidate"),
            "confidence": state.get("regime_confidence"),
            "updated_at": state.get("updated_at"),
        })
    if state.get("strategy"):
        items.append({
            "event": "STRATEGY_STATE",
            "mode": active_mode,
            "symbol": state.get("last_symbol"),
            "strategy": state.get("strategy"),
            "trace": state.get("last_strategy_trace"),
            "updated_at": state.get("updated_at"),
        })

    for row in rows:
        clean = {k: v for k, v in row.items() if k != "_id"}
        status = str(row.get("status") or "").upper()
        clean["event"] = "POSITION_OPENED" if status == "OPEN" else "POSITION_CLOSED"
        clean["mode"] = active_mode
        items.append(clean)
        if len(items) >= limit:
            break

    return {
        "mode": active_mode,
        "items": items[:limit],
        "count": min(len(items), limit),
        "engine": {k: v for k, v in state.items() if k != "_id"},
    }
