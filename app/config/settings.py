from functools import lru_cache
from typing import Literal
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    environment: Literal['development','test','staging','production'] = 'development'
    log_level: str = 'INFO'
    trading_worker_enabled: bool = False
    engine_heartbeat_seconds: float = Field(900.0, ge=60.0, le=3600.0)
    cors_allowed_origins: str = ''
    coinw_rest_base_url: str = 'https://api.coinw.com'
    coinw_ws_url: str = 'wss://ws.futurescw.com/perpum'
    mongodb_uri: str = ''
    mongodb_database: str = 'kaeleon'
    default_symbol: str = 'BTC'
    default_timeframe: str = '5m'
    paper_initial_equity: float = Field(100, gt=0)
    paper_taker_fee: float = Field(.0006, ge=0)
    paper_maker_fee: float = Field(.0001, ge=0)
    paper_slippage_bps: float = Field(2.0, ge=0)
    paper_max_spread_bps: float = Field(30.0, ge=0)
    trade_target_front_run_ratio: float = Field(0.92, ge=0.80, le=1.0)
    trade_break_even_activation_ratio: float = Field(0.55, ge=0.20, le=0.90)
    trade_profit_lock_activation_ratio: float = Field(0.80, ge=0.40, le=0.98)
    trade_profit_lock_capture_ratio: float = Field(0.35, ge=0.05, le=0.80)
    trade_exit_fee_rate_estimate: float = Field(0.0006, ge=0.0, le=0.01)
    trade_break_even_buffer_bps: float = Field(3.0, ge=0.0, le=50.0)
    # Entry-quality / anti-reentry protections. These defaults intentionally favor
    # fewer, cleaner entries over rapid re-entry after a failed setup.
    trade_post_loss_global_cooldown_seconds: float = Field(900.0, ge=0.0, le=7200.0)
    trade_post_loss_symbol_cooldown_seconds: float = Field(1800.0, ge=0.0, le=14400.0)
    trade_entry_max_chase_atr: float = Field(0.20, ge=0.0, le=2.0)
    trade_entry_max_adverse_reversal_atr: float = Field(0.15, ge=0.0, le=2.0)
    trade_entry_min_stop_atr: float = Field(0.55, ge=0.0, le=5.0)
    trade_entry_min_stop_spreads: float = Field(3.0, ge=0.0, le=20.0)
    trade_entry_orderbook_conflict_threshold: float = Field(0.35, ge=0.0, le=0.95)
    market_poll_seconds: float = Field(2.0, gt=0)
    market_scanner_depth: int = Field(12, ge=1, le=50)
    market_scanner_parallel: int = Field(3, ge=1, le=10)
    market_scanner_cache_seconds: float = Field(30.0, gt=0)
    fixed_leverage: int = Field(10, ge=10, le=10)
    min_operating_capital: float = Field(3.0, gt=0)
    max_active_users: int = Field(1000, ge=1, le=10000)
    user_runtime_refresh_seconds: float = Field(10.0, gt=0)
    engine_state_persist_seconds: float = Field(15.0, ge=2.0, le=300.0)
    trade_persist_timeout_seconds: float = Field(4.0, ge=0.5, le=15.0)
    trade_persist_retries: int = Field(2, ge=1, le=5)
    regime_structure_weight: float = Field(.25, ge=0, le=1)
    regime_momentum_weight: float = Field(.20, ge=0, le=1)
    regime_volatility_weight: float = Field(.20, ge=0, le=1)
    regime_liquidity_weight: float = Field(.20, ge=0, le=1)
    regime_breadth_weight: float = Field(.15, ge=0, le=1)

    credential_encryption_key: str = ''

    # admin (from admin_ui / admin_data_views branch)
    admin_phone: str = ''

    # billing / subscriptions (from billing_referrals branch)
    live_trial_days: int = Field(5, ge=1, le=30)
    payment_wallet: str = ''
    payment_network: str = 'BNB_SMART_CHAIN'
    bsc_rpc_url: str = 'https://bsc-dataseed.binance.org/'
    # Binance-Peg USDT on BNB Smart Chain; override with USDT_BSC_CONTRACT if needed.
    usdt_bsc_contract: str = '0x55d398326f99059fF775485246999027B3197955'
    usdt_decimals: int = Field(18, ge=0, le=36)

    # telegram registration verification
    telegram_enabled: bool = False
    telegram_bot_token: str = ''
    telegram_bot_username: str = ''
    telegram_webhook_url: str = ''
    telegram_webhook_secret: str = ''
    telegram_auto_set_webhook: bool = True
    telegram_api_timeout_seconds: float = Field(10.0, gt=0, le=60)

    @property
    def telegram_registration_configured(self) -> bool:
        required = bool(
            self.telegram_bot_token
            and self.telegram_bot_username
            and self.telegram_webhook_secret
        )
        if self.telegram_auto_set_webhook:
            required = required and bool(self.telegram_webhook_url)
        return required

@lru_cache
def get_settings() -> Settings:
    return Settings()
