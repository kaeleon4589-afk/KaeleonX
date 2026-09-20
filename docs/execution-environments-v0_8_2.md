# KAELEON Execution Environments v0.12.0

## Definitive model

KAELEON has exactly two user-facing trading environments:

- **DEMO**: internal KAELEON simulation. It consumes real CoinW market data and uses virtual capital. It never submits orders to CoinW.
- **LIVE**: real CoinW execution using the same CoinW market-data source and the same strategy/risk/decision pipeline as DEMO.

The trading environment, activation state and operating capital are **per user**. There is no global CoinW trading account configured in Railway.

## CoinW credentials

Each user supplies their own CoinW API Key + API Secret from their account settings. KAELEON encrypts both values at rest using the server-only `CREDENTIAL_ENCRYPTION_KEY`. The encrypted values are stored in the user's `user_trading_profiles` document and are never returned to the browser.

The exchange API key/secret are therefore **not** Railway variables. Only the encryption master key belongs in Railway.

## Operating capital

Each user chooses the capital that KAELEON is allowed to use for automated trading. Risk calculations use:

`effective_capital = min(user_configured_operating_capital, CoinW_available_equity)`

This means a user can have more funds in CoinW than the configured trading capital; the unused balance is not treated as trading capital by the KAELEON risk engine.

The platform minimum is 3 units of the configured account currency (currently USDT for the futures account).

## One operation per user

A user runtime owns its own `PositionManager` and `TradingOrchestrator`, and the orchestrator blocks new entries whenever any open position already exists for that user. Exchange reconciliation remains authoritative in LIVE mode.

## Fixed controls

Leverage remains an internal KAELEON setting. Markets and timeframes remain platform-managed and are not user-selectable trading-engine controls.
