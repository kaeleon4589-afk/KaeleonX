# KAELEON Manual Trading Terminal

## Scope

This phase adds user-initiated futures trading to the existing KAELEON market chart while keeping the automatic strategy engine intact.

The four execution/origin combinations are kept explicit:

- `DEMO + BOT`
- `DEMO + MANUAL`
- `LIVE + BOT`
- `LIVE + MANUAL`

`source=MANUAL` is persisted on manual positions so bot statistics and manual actions can be distinguished.

## Frontend

The existing `LiveMarketChart` remains the market/visualization engine. `TradingTerminal` wraps it with:

- account bar: mode, balance, available balance, used margin, unrealized PnL and equity;
- manual order ticket;
- `MARKET` and `LIMIT`;
- LONG / SHORT;
- margin amount;
- leverage;
- mandatory TP and SL;
- open positions with Entry, Margin, PnL, ROI, liquidation price (when CoinW provides it) and TP/SL;
- open manual orders;
- closed manual trade history with Entry/Exit, net PnL and ROI;
- manual order audit history;
- manual close;
- TP/SL editing;
- explicit confirmation modal for LIVE order, close, cancel and TP/SL changes.

Desktop uses chart + order ticket columns. Mobile stacks the order ticket below the responsive chart.

## Backend API

Authenticated user routes:

- `GET /api/user/manual-trading/state`
- `POST /api/user/manual-trading/orders`
- `POST /api/user/manual-trading/orders/{manual_order_id}/cancel`
- `POST /api/user/manual-trading/positions/{position_id}/close`
- `PUT /api/user/manual-trading/positions/{position_id}/protection`

The configured execution mode must match the requested manual mode.

## DEMO execution

DEMO never calls a private CoinW trading endpoint.

- Market data still comes from CoinW.
- MARKET entries use the current bid/ask and the existing paper slippage/fee model.
- LIMIT orders are persisted in `manual_orders` and monitored by the server worker; they do not depend on the browser remaining open.
- Pending LIMIT margin is reserved from available DEMO balance.
- Open manual margin and pending manual LIMIT reservations also reduce capital available to the automatic DEMO bot, avoiding double allocation.
- TP/SL positions are monitored by the existing `PositionManager`.
- Manual closes calculate realized PnL and exit fee and persist the closed position.

## LIVE execution

LIVE sends orders only from the backend using the server-side CoinW credentials.

KAELEON uses:

- quote-currency order size (`quantityUnit=0`);
- isolated margin (`positionModel=0`) in this phase;
- `execute` for MARKET;
- `plan` for LIMIT;
- exchange-side Stop Loss and Take Profit;
- CoinW position/order reconciliation before treating an accepted request as filled.

A returned CoinW order ID is stored as accepted/pending; it is not treated as a fill by itself. If a MARKET position is confirmed but TP/SL confirmation fails, the position is persisted as open with `protected=false` and the terminal surfaces a high-visibility warning instead of reporting a fully protected success.

### LIVE order lifecycle

Typical lifecycle:

`SUBMITTING -> ACCEPTED/OPEN -> FILLED -> OPEN POSITION -> CLOSED`

Additional states include `PARTIALLY_FILLED`, `CANCELLED`, `REJECTED` and `UNKNOWN`.

`UNKNOWN` is deliberate. If a network failure makes the result of a submit ambiguous, KAELEON does not blindly retry the order.

## Financial idempotency

Every UI submit uses a unique `client_order_id` beginning with `MANUAL-`.

The backend atomically claims `(user_id, client_order_id)` before the order side effect. A duplicate request returns the already claimed order instead of submitting a second one. MongoDB also has a unique compound index for this key.

This protects against double-clicks, browser retries and duplicate HTTP delivery.

## Security boundary

CoinW API secrets remain backend-only. The browser never receives API credentials.

LIVE requires:

- authenticated KAELEON user;
- active LIVE entitlement;
- configured and verified CoinW credentials;
- active UI mode `LIVE`;
- explicit confirmation of every money-moving manual action (default production behavior).

Manual routes only permit closing/editing positions whose persisted `source` is `MANUAL`; the manual UI cannot accidentally close a BOT position.

## Environment variables

```env
MANUAL_TRADING_ENABLED=true
MANUAL_TRADING_MIN_MARGIN=1.0
MANUAL_TRADING_MAX_LEVERAGE=50
MANUAL_LIVE_CONFIRMATION_REQUIRED=true
```

The automatic bot still uses its independent `FIXED_LEVERAGE` setting.

## Current deliberate limits

This first production-safe version implements `MARKET` and `LIMIT`, isolated margin, full manual close and full-position TP/SL.

Not yet implemented in the manual terminal:

- partial close percentages;
- Cross margin;
- trailing stop;
- trigger/conditional order ticket;
- drag-to-edit TP/SL directly on the chart;
- CoinW maintenance-margin-tier based DEMO liquidation simulation.

Those features can be layered on without changing the source/mode separation introduced here.
