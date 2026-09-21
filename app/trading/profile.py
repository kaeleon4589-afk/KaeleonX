from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from app.execution.environments import CoinWCredentials, TradingEnvironment
from app.security.credential_vault import CredentialVault


TradingMode = Literal["demo", "live"]


@dataclass(frozen=True)
class UserTradingProfile:
    user_id: str
    execution_mode: TradingMode
    trading_enabled: bool
    operating_capital: float
    demo_operating_capital: float
    live_operating_capital: float
    coinw_configured: bool
    coinw_verified: bool
    coinw_api_key_masked: str | None
    coinw_verified_at: object | None = None
    coinw_available_equity: float = 0.0
    updated_at: object | None = None


class UserTradingProfileService:
    COLLECTION = "user_trading_profiles"

    def __init__(self, db, vault: CredentialVault, minimum_operating_capital: float = 3.0):
        self.db = db
        self.vault = vault
        self.minimum_operating_capital = float(minimum_operating_capital)

    @staticmethod
    def _mode(value: str | None) -> str:
        mode = str(value or "demo").lower()
        if mode not in {TradingEnvironment.DEMO.value, TradingEnvironment.LIVE.value}:
            raise ValueError("invalid_execution_mode")
        return mode

    def _capital_for_mode(self, row: dict, mode: str) -> float:
        key = f"{mode}_operating_capital"
        if key in row:
            return float(row.get(key) or 0.0)
        # Backwards compatibility for profiles created before mode-separated capital.
        legacy_mode = str(row.get("execution_mode", "demo"))
        if legacy_mode == mode and row.get("operating_capital") is not None:
            return float(row.get("operating_capital") or 0.0)
        return 0.0

    def get(self, user_id: str) -> dict:
        return self.db.find_one(self.COLLECTION, {"user_id": user_id}) or {}

    def public(self, user_id: str) -> UserTradingProfile:
        row = self.get(user_id)
        mode = self._mode(row.get("execution_mode", "demo"))
        api_key_encrypted = row.get("coinw_api_key_encrypted")
        masked = None
        if api_key_encrypted:
            masked = self.vault.mask(self.vault.decrypt(api_key_encrypted))
        demo_capital = self._capital_for_mode(row, "demo")
        live_capital = self._capital_for_mode(row, "live")
        configured = bool(row.get("coinw_api_key_encrypted") and row.get("coinw_api_secret_encrypted"))
        return UserTradingProfile(
            user_id=user_id,
            execution_mode=mode,
            trading_enabled=bool(row.get("trading_enabled", False)),
            operating_capital=demo_capital if mode == "demo" else live_capital,
            demo_operating_capital=demo_capital,
            live_operating_capital=live_capital,
            coinw_configured=configured,
            coinw_verified=bool(configured and row.get("coinw_verified", False)),
            coinw_api_key_masked=masked,
            coinw_verified_at=row.get("coinw_verified_at"),
            coinw_available_equity=float(row.get("coinw_available_equity") or 0.0),
            updated_at=row.get("updated_at"),
        )

    def save(
        self,
        user_id: str,
        *,
        execution_mode: str,
        trading_enabled: bool,
        operating_capital: float | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
    ) -> dict:
        mode = self._mode(execution_mode)
        existing = self.get(user_id)
        existing_mode = self._mode(existing.get("execution_mode", "demo"))
        key_enc = existing.get("coinw_api_key_encrypted")
        secret_enc = existing.get("coinw_api_secret_encrypted")
        open_positions = self.db.find_many(
            "positions", {"user_id": user_id, "status": "OPEN", "mode": existing_mode}, limit=1
        )

        if existing_mode == "live" and mode != "live" and open_positions:
            raise ValueError("close_live_position_before_switching_to_demo")

        credentials_changed = api_key is not None or api_secret is not None
        if credentials_changed:
            if existing_mode == "live" and (bool(existing.get("trading_enabled")) or open_positions):
                raise ValueError("disable_live_trading_and_close_position_before_changing_credentials")
            if not (str(api_key or "").strip() and str(api_secret or "").strip()):
                raise ValueError("coinw_api_key_and_secret_required_together")
            key_enc = self.vault.encrypt(str(api_key).strip())
            secret_enc = self.vault.encrypt(str(api_secret).strip())

        configured = bool(key_enc and secret_enc)
        verified = bool(existing.get("coinw_verified", False)) and configured and not credentials_changed

        demo_capital = self._capital_for_mode(existing, "demo")
        live_capital = self._capital_for_mode(existing, "live")
        if operating_capital is not None:
            capital = float(operating_capital)
            if capital < self.minimum_operating_capital:
                raise ValueError("operating_capital_below_platform_minimum")
            if not verified:
                raise ValueError("coinw_verification_required_for_capital")
            if mode == "demo":
                demo_capital = capital
            else:
                live_capital = capital

        active_capital = demo_capital if mode == "demo" else live_capital
        if trading_enabled:
            if not verified:
                raise ValueError("coinw_verification_required_for_trading")
            if active_capital < self.minimum_operating_capital:
                raise ValueError("operating_capital_required_before_trading")

        document = {
            "user_id": user_id,
            "execution_mode": mode,
            "trading_enabled": bool(trading_enabled),
            "operating_capital": active_capital,  # compatibility with older readers
            "demo_operating_capital": demo_capital,
            "live_operating_capital": live_capital,
            "coinw_api_key_encrypted": key_enc,
            "coinw_api_secret_encrypted": secret_enc,
            "coinw_verified": verified,
        }
        if credentials_changed:
            document.update({
                "coinw_verified_at": None,
                "coinw_available_equity": 0.0,
            })
        self.db.upsert(self.COLLECTION, {"user_id": user_id}, document)
        return self.get(user_id)

    def mark_verified(self, user_id: str, available_equity: float) -> dict:
        row = self.get(user_id)
        if not row.get("coinw_api_key_encrypted") or not row.get("coinw_api_secret_encrypted"):
            raise ValueError("coinw_credentials_not_configured")
        self.db.upsert(self.COLLECTION, {"user_id": user_id}, {
            "coinw_verified": True,
            "coinw_verified_at": datetime.now(timezone.utc),
            "coinw_available_equity": max(0.0, float(available_equity)),
        })
        return self.get(user_id)

    def update_available_equity(self, user_id: str, available_equity: float) -> None:
        self.db.upsert(self.COLLECTION, {"user_id": user_id}, {
            "coinw_available_equity": max(0.0, float(available_equity)),
        })

    def set_credentials(self, user_id: str, api_key: str, api_secret: str) -> dict:
        existing = self.get(user_id)
        mode = existing.get("execution_mode", "demo")
        enabled = bool(existing.get("trading_enabled", False))
        return self.save(
            user_id,
            execution_mode=mode,
            trading_enabled=enabled,
            operating_capital=None,
            api_key=api_key,
            api_secret=api_secret,
        )

    def clear_credentials(self, user_id: str) -> dict:
        existing = self.get(user_id)
        if not existing:
            return {}
        if str(existing.get("execution_mode", "demo")) == "live" and bool(existing.get("trading_enabled")):
            raise ValueError("pause_live_before_removing_credentials")
        self.db.upsert(self.COLLECTION, {"user_id": user_id}, {
            "trading_enabled": False,
            "coinw_api_key_encrypted": None,
            "coinw_api_secret_encrypted": None,
            "coinw_verified": False,
            "coinw_verified_at": None,
            "coinw_available_equity": 0.0,
        })
        return self.get(user_id)

    def credentials(self, user_id: str) -> CoinWCredentials:
        row = self.get(user_id)
        key_enc = row.get("coinw_api_key_encrypted")
        secret_enc = row.get("coinw_api_secret_encrypted")
        if not key_enc or not secret_enc:
            raise ValueError("coinw_credentials_not_configured")
        key = self.vault.decrypt(key_enc)
        secret = self.vault.decrypt(secret_enc)
        creds = CoinWCredentials(key, secret)
        creds.validate()
        return creds
