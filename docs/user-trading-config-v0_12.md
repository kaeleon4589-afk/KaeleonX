# KAELEON User Trading Configuration v0.12.0

## User-owned CoinW credentials

Authenticated users configure their own CoinW API Key and API Secret through `/user/trading-config`.

The browser may submit the values only over HTTPS. The API immediately encrypts both fields using `CREDENTIAL_ENCRYPTION_KEY` before persistence. API secrets are never returned by any user endpoint.

`GET /user/trading-config` returns only whether credentials are configured and a masked API key.

## Operating capital

The user configures `operating_capital`. The platform minimum is 3.

Risk uses the configured amount, capped by the account's current available equity before a LIVE decision:

`effective_capital = min(configured_capital, available_equity)`

If effective capital falls below the minimum, no new trade is opened.

## Execution toggle

`trading_enabled=false` is the safe default. A user must explicitly enable automated trading.

Disabling trading stops new entries but keeps the runtime available for LIVE reconciliation/protection of an already-open exchange position.

## LIVE entitlement

Switching the account to LIVE is allowed only when the billing entitlement says `live_allowed=true`.

## API endpoints

- `GET /user/trading-config`
- `PUT /user/trading-config`
- `POST /user/coinw/test`
- `DELETE /user/coinw-credentials`
