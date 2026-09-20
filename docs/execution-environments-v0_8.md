# KAELEON Execution Environments v0.12.0

## User-owned execution

KAELEON exposes two user-facing environments:

- **DEMO**: internal KAELEON simulation using real CoinW market data. It never submits orders to CoinW.
- **LIVE**: real CoinW execution using the same strategy/risk/decision pipeline.

The execution mode is configured per authenticated user. There is no platform-wide CoinW API key/secret.

## Credentials

Each user provides their CoinW API Key and API Secret through the authenticated account settings. Both values are encrypted at rest using the server-only `CREDENTIAL_ENCRYPTION_KEY`.

## Operating capital

Each user selects the capital KAELEON is allowed to use. The LIVE risk engine uses the lower of the configured capital and current available CoinW equity, so unused account balance remains outside the trading-capital limit.

## Platform-owned controls

Leverage, market selection and timeframe selection remain controlled by KAELEON's internal strategy/risk configuration.
