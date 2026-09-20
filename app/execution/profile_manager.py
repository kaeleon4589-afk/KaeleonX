from __future__ import annotations

from dataclasses import dataclass

from app.execution.environments import CoinWCredentials, CoinWEnvironmentConfig, TradingEnvironment


@dataclass
class UserCoinWProfile:
    """One CoinW credential profile shared by DEMO and LIVE market-data access.

    DEMO never sends orders to CoinW. LIVE may send orders after explicit activation.
    Keeping one credential pair guarantees both environments observe the same market source.
    """

    credentials: CoinWCredentials
    rest_base_url: str = "https://api.coinw.com"

    def validate(self) -> None:
        self.credentials.validate()
        if not self.rest_base_url.strip():
            raise ValueError("coinw_rest_base_url_required")


@dataclass
class UserCoinWProfiles:
    profile: UserCoinWProfile | None = None

    def set(self, api_key: str, api_secret: str, rest_base_url: str = "https://api.coinw.com") -> None:
        profile = UserCoinWProfile(CoinWCredentials(api_key, api_secret), rest_base_url)
        profile.validate()
        self.profile = profile

    def require(self, environment: TradingEnvironment) -> CoinWEnvironmentConfig:
        if self.profile is None:
            raise ValueError("coinw_credentials_not_configured")
        self.profile.validate()
        return CoinWEnvironmentConfig(environment, self.profile.credentials, self.profile.rest_base_url)
