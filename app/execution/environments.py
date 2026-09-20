from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TradingEnvironment(str, Enum):
    """KAELEON account environments.

    DEMO is KAELEON's internal simulation using real CoinW market data.
    LIVE is the same strategy/risk pipeline with real CoinW execution.
    """

    DEMO = "demo"
    LIVE = "live"


@dataclass(frozen=True)
class CoinWCredentials:
    api_key: str
    api_secret: str

    def validate(self) -> None:
        if not self.api_key.strip() or not self.api_secret.strip():
            raise ValueError("coinw_api_key_and_secret_required")

    @property
    def masked_key(self) -> str:
        value = self.api_key
        if len(value) <= 8:
            return "••••••••"
        return f"{value[:4]}••••{value[-4:]}"


@dataclass(frozen=True)
class CoinWEnvironmentConfig:
    environment: TradingEnvironment
    credentials: CoinWCredentials
    rest_base_url: str = "https://api.coinw.com"

    def validate(self) -> None:
        self.credentials.validate()
        if not self.rest_base_url.strip():
            raise ValueError("coinw_rest_base_url_required")
