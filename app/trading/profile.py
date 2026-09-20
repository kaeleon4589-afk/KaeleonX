from __future__ import annotations

from dataclasses import dataclass
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
    coinw_configured: bool
    coinw_api_key_masked: str | None
    updated_at: object | None = None


class UserTradingProfileService:
    COLLECTION = "user_trading_profiles"

    def __init__(self, db, vault: CredentialVault, minimum_operating_capital: float = 3.0):
        self.db = db
        self.vault = vault
        self.minimum_operating_capital = float(minimum_operating_capital)

    def _validate(self, operating_capital: float, execution_mode: str) -> tuple[str, float]:
        mode = str(execution_mode or "demo").lower()
        if mode not in {TradingEnvironment.DEMO.value, TradingEnvironment.LIVE.value}:
            raise ValueError("invalid_execution_mode")
        capital = float(operating_capital)
        if capital < self.minimum_operating_capital:
            raise ValueError("operating_capital_below_platform_minimum")
        return mode, capital

    def get(self, user_id: str) -> dict:
        profile = self.db.find_one(self.COLLECTION, {"user_id": user_id}) or {}
        return profile

    def public(self, user_id: str) -> UserTradingProfile:
        row = self.get(user_id)
        api_key_encrypted = row.get("coinw_api_key_encrypted")
        masked = None
        if api_key_encrypted:
            masked = self.vault.mask(self.vault.decrypt(api_key_encrypted))
        return UserTradingProfile(
            user_id=user_id,
            execution_mode=str(row.get("execution_mode", "demo")),
            trading_enabled=bool(row.get("trading_enabled", False)),
            operating_capital=float(row.get("operating_capital", self.minimum_operating_capital)),
            coinw_configured=bool(row.get("coinw_api_key_encrypted") and row.get("coinw_api_secret_encrypted")),
            coinw_api_key_masked=masked,
            updated_at=row.get("updated_at"),
        )

    def save(
        self,
        user_id: str,
        *,
        execution_mode: str,
        trading_enabled: bool,
        operating_capital: float,
        api_key: str | None = None,
        api_secret: str | None = None,
    ) -> dict:
        mode, capital = self._validate(operating_capital, execution_mode)
        existing = self.get(user_id)
        existing_mode = str(existing.get("execution_mode", "demo"))
        key_enc = existing.get("coinw_api_key_encrypted")
        secret_enc = existing.get("coinw_api_secret_encrypted")
        open_positions = self.db.find_many("positions", {"user_id": user_id, "status": "OPEN"}, limit=1)

        if existing_mode == "live" and mode != "live" and open_positions:
            raise ValueError("close_live_position_before_switching_to_demo")

        if api_key is not None or api_secret is not None:
            if existing_mode == "live" and (bool(existing.get("trading_enabled")) or open_positions):
                raise ValueError("disable_live_trading_and_close_position_before_changing_credentials")
            if not (str(api_key or "").strip() and str(api_secret or "").strip()):
                raise ValueError("coinw_api_key_and_secret_required_together")
            key_enc = self.vault.encrypt(api_key.strip())
            secret_enc = self.vault.encrypt(api_secret.strip())

        if mode == "live" and not (key_enc and secret_enc):
            raise ValueError("coinw_credentials_required_for_live")

        document = {
            "user_id": user_id,
            "execution_mode": mode,
            "trading_enabled": bool(trading_enabled),
            "operating_capital": capital,
            "coinw_api_key_encrypted": key_enc,
            "coinw_api_secret_encrypted": secret_enc,
        }
        self.db.upsert(self.COLLECTION, {"user_id": user_id}, document)
        return self.get(user_id)

    def set_credentials(self, user_id: str, api_key: str, api_secret: str) -> dict:
        existing = self.get(user_id)
        mode = existing.get("execution_mode", "demo")
        capital = float(existing.get("operating_capital", self.minimum_operating_capital))
        enabled = bool(existing.get("trading_enabled", False))
        return self.save(
            user_id,
            execution_mode=mode,
            trading_enabled=enabled,
            operating_capital=capital,
            api_key=api_key,
            api_secret=api_secret,
        )

    def clear_credentials(self, user_id: str) -> dict:
        existing = self.get(user_id)
        if not existing:
            return {}
        if str(existing.get("execution_mode", "demo")) == "live":
            raise ValueError("switch_to_demo_before_removing_credentials")
        self.db.upsert(self.COLLECTION, {"user_id": user_id}, {
            "coinw_api_key_encrypted": None,
            "coinw_api_secret_encrypted": None,
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
