from typing import Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.config.settings import get_settings
from app.auth.service import AuthService
from app.billing.service import BillingService
from app.coinw.account import CoinWAccountAPI
from app.coinw.rest_client import CoinWRestClient
from app.security.credential_vault import CredentialVault
from app.storage.database import Database
from app.trading.profile import UserTradingProfileService

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
        _profiles = UserTradingProfileService(
            _db,
            CredentialVault(s.credential_encryption_key),
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


def owned_query(user_id, extra=None):
    q = {"user_id": user_id}
    if extra:
        q.update(extra)
    return q


def collection(db, name, query):
    if db.db:
        return list(db.db[name].find(query, {"_id": 0}).sort("created_at", -1).limit(50))
    rows = [d for c, d in db.memory if c == name and all(d.get(k) == v for k, v in query.items())]
    return list(reversed(rows[-50:]))


class TradingConfigRequest(BaseModel):
    execution_mode: Literal["demo", "live"] = "demo"
    trading_enabled: bool = False
    operating_capital: float = Field(gt=0)
    coinw_api_key: str | None = Field(default=None, min_length=1)
    coinw_api_secret: str | None = Field(default=None, min_length=1)


@router.get("/trading-config")
def trading_config(authorization: str | None = Header(default=None)):
    _, user, profiles, billing = current(authorization)
    profile = profiles.public(user["user_id"])
    entitlement = billing.entitlement(user["user_id"])
    return {
        "execution_mode": profile.execution_mode,
        "trading_enabled": profile.trading_enabled,
        "operating_capital": profile.operating_capital,
        "minimum_operating_capital": profiles.minimum_operating_capital,
        "coinw_configured": profile.coinw_configured,
        "coinw_api_key": profile.coinw_api_key_masked,
        "live_allowed": entitlement.live_allowed,
        "live_state": entitlement.live_state,
    }


@router.put("/trading-config")
def save_trading_config(req: TradingConfigRequest, authorization: str | None = Header(default=None)):
    _, user, profiles, billing = current(authorization)
    if req.execution_mode == "live":
        if not billing.entitlement(user["user_id"]).live_allowed:
            raise HTTPException(403, "live_not_entitled")
    try:
        row = profiles.save(
            user["user_id"],
            execution_mode=req.execution_mode,
            trading_enabled=req.trading_enabled,
            operating_capital=req.operating_capital,
            api_key=req.coinw_api_key,
            api_secret=req.coinw_api_secret,
        )
        public = profiles.public(user["user_id"])
        return {
            "saved": True,
            "execution_mode": public.execution_mode,
            "trading_enabled": public.trading_enabled,
            "operating_capital": public.operating_capital,
            "coinw_configured": public.coinw_configured,
            "coinw_api_key": public.coinw_api_key_masked,
            "profile_updated_at": row.get("updated_at"),
        }
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.delete("/coinw-credentials")
def delete_coinw_credentials(authorization: str | None = Header(default=None)):
    _, user, profiles, _ = current(authorization)
    try:
        profiles.clear_credentials(user["user_id"])
        return {"deleted": True, "coinw_configured": False}
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.post("/coinw/test")
async def test_coinw_credentials(authorization: str | None = Header(default=None)):
    _, user, profiles, _ = current(authorization)
    settings = get_settings()
    try:
        creds = profiles.credentials(user["user_id"])
        client = CoinWRestClient(settings.coinw_rest_base_url, creds.api_key, creds.api_secret)
        account = CoinWAccountAPI(client)
        response = await account.assets("usdt")
        data = response.get("data", response) if isinstance(response, dict) else response
        if isinstance(data, dict):
            available = float(data.get("availableUsdt") or data.get("availableMargin") or 0.0)
        else:
            available = 0.0
        return {"connected": True, "available_equity": available, "api_key": profiles.public(user["user_id"]).coinw_api_key_masked}
    except Exception as exc:
        raise HTTPException(400, f"coinw_connection_failed:{exc}")


@router.get("/dashboard")
def dashboard(authorization: str | None = Header(default=None)):
    db, user, profiles, _ = current(authorization)
    uid = user["user_id"]
    state = db.find_one("user_engine_state", {"user_id": uid}) or {
        "mode": "DEMO", "status": "PAUSADO", "capital": 0.0,
        "pnl": 0.0, "pnl_pct": 0.0, "regime": "TRANSITION",
        "direction": "NEUTRAL", "strategy": None, "open_position": None,
    }
    profile = profiles.public(uid)
    state["configured_capital"] = profile.operating_capital
    state["trading_enabled"] = profile.trading_enabled
    return {"user": {"user_id": uid, "role": user.get("role")}, "engine": state}


@router.get("/execution")
def execution(authorization: str | None = Header(default=None)):
    db, user, profiles, _ = current(authorization)
    state = db.find_one("user_engine_state", {"user_id": user["user_id"]}) or {}
    profile = profiles.public(user["user_id"])
    return {
        "mode": state.get("mode", profile.execution_mode.upper()),
        "status": state.get("status", "PAUSADO" if not profile.trading_enabled else "ANALIZANDO"),
        "capital": state.get("capital", profile.operating_capital),
        "configured_capital": profile.operating_capital,
        "markets_scanned": state.get("markets_scanned", 0),
        "candidates": state.get("candidates", 0),
        "coinw_connected": bool(state.get("coinw_connected", profile.coinw_configured and profile.execution_mode == "live")),
        "leverage": "INTERNAL",
        "market_selection": "KAELEON_AUTO",
        "timeframe_selection": "KAELEON_INTERNAL",
        "trading_enabled": profile.trading_enabled,
    }


@router.get("/operations")
def operations(authorization: str | None = Header(default=None)):
    db, user, _, _ = current(authorization); uid = user["user_id"]
    return {
        "open": collection(db, "positions", owned_query(uid, {"status": "OPEN"})),
        "closed": collection(db, "positions", owned_query(uid, {"status": {"$ne": "OPEN"}})) if db.db else collection(db, "positions", {"user_id": uid}),
    }


@router.get("/performance")
def performance(authorization: str | None = Header(default=None)):
    db, user, profiles, _ = current(authorization); uid = user["user_id"]
    state = db.find_one("user_engine_state", {"user_id": uid}) or {}
    profile = profiles.public(uid)
    pnl = float(state.get("pnl", 0.0)); capital = float(state.get("capital", profile.operating_capital))
    return {
        "capital": capital,
        "configured_capital": profile.operating_capital,
        "current_capital": capital + pnl,
        "pnl": pnl,
        "pnl_pct": float(state.get("pnl_pct", (pnl / capital * 100) if capital else 0)),
        "drawdown": float(state.get("drawdown", 0.0)),
        "win_rate": float(state.get("win_rate", 0.0)),
        "profit_factor": float(state.get("profit_factor", 0.0)),
        "trades": int(state.get("trades", 0)),
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
        "coinw_api_key": profile.coinw_api_key_masked,
    }
