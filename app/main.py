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
from app.trading.worker_lease import WorkerLease


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
        initial_demo_equity=s.paper_initial_equity,
    )
    runtimes = UserTradingRuntimeManager(s, db, audit, profiles)
    client = CoinWMarketClient(s.coinw_rest_base_url)
    scanner = CoinWMarketScanner(
        client, depth=s.market_scanner_depth, cache_seconds=s.market_scanner_cache_seconds, audit=audit
    )
    market = MultiMarketCoordinator(
        client, scanner, poll_seconds=s.market_poll_seconds, audit=audit,
        max_parallel=s.market_scanner_parallel, heartbeat_seconds=s.engine_heartbeat_seconds,
    )

    lease = WorkerLease(db)
    if not await asyncio.to_thread(lease.renew):
        await client.close()
        raise RuntimeError('another_trading_worker_is_active')
    runtimes.entry_guard = lease.valid

    async def on_snapshot(snapshot):
        await runtimes.run_snapshot(snapshot)

    try:
        await runtimes.refresh()
        async with asyncio.TaskGroup() as group:
            group.create_task(lease.run())
            group.create_task(market.run(on_snapshot))
            group.create_task(market.monitor(on_snapshot, runtimes.tracked_symbols))
            group.create_task(runtimes.notifier.run())
    finally:
        await client.close()
        # Finish already queued business writes before releasing worker ownership.
        tasks = list(runtimes.notifier._tasks)
        for runtime in runtimes._runtimes.values():
            tasks.extend(runtime.orchestrator.persistence._background)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(lease.release)


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
