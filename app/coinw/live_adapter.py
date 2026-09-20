from __future__ import annotations

from .rest_client import CoinWRestClient
from .orders import CoinWOrdersAPI
from .positions import CoinWPositionsAPI
from .account import CoinWAccountAPI
from .executor import CoinWExecutor


def build_coinw_adapter_from_credentials(api_key: str, api_secret: str, base_url: str = "https://api.coinw.com", audit=None, enabled: bool = True):
    if not api_key or not api_secret:
        raise ValueError("coinw_api_key_and_secret_required")
    client = CoinWRestClient(base_url, api_key, api_secret)
    orders = CoinWOrdersAPI(client)
    positions = CoinWPositionsAPI(client)
    account = CoinWAccountAPI(client)
    return CoinWExecutor(orders, positions, account, audit=audit, enabled=enabled)


def build_live_adapter_from_credentials(api_key: str, api_secret: str, base_url: str = "https://api.coinw.com", audit=None):
    return build_coinw_adapter_from_credentials(api_key, api_secret, base_url=base_url, audit=audit, enabled=True)
