import asyncio

from app.config.settings import get_settings
from app.logging.logger import get_logger, AuditLogger
from app.storage.database import Database
from app.coinw.market import CoinWMarketClient
from app.market.coordinator import MultiMarketCoordinator
from app.market.scanner import CoinWMarketScanner
from app.security.credential_vault import CredentialVault
from app.trading.profile import UserTradingProfileService
from app.trading.runtime import UserTradingRuntimeManager


async def run():
    s = get_settings()
    logger = get_logger("kaeleon")
    if s.environment == "production" and not s.mongodb_uri.strip():
        raise RuntimeError("mongodb_uri_required_in_production")
    logger.info(
        "KAELEON starting | environment=%s | market=AUTO_COINW | timeframe=%s",
        s.environment,
        s.default_timeframe,
    )

    db = Database(s.mongodb_uri, s.mongodb_database)
    audit = AuditLogger(db)
    if not s.credential_encryption_key.strip():
        raise RuntimeError("credential_encryption_key_required")

    profiles = UserTradingProfileService(
        db,
        CredentialVault(s.credential_encryption_key),
        minimum_operating_capital=s.min_operating_capital,
    )
    runtimes = UserTradingRuntimeManager(s, db, audit, profiles)
    client = CoinWMarketClient(s.coinw_rest_base_url, max_rps=s.coinw_public_max_rps)
    scanner = CoinWMarketScanner(
        client, depth=s.market_scanner_depth, cache_seconds=s.market_scanner_cache_seconds, audit=audit
    )
    market = MultiMarketCoordinator(
        client, scanner, poll_seconds=s.market_poll_seconds, audit=audit,
        max_parallel=s.market_scanner_parallel, heartbeat_seconds=s.engine_heartbeat_seconds,
    )

    async def on_snapshot(snapshot):
        await runtimes.run_snapshot(snapshot)

    try:
        await market.run(on_snapshot)
    finally:
        await client.aclose()


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
