# KAELEON observability (lean production mode)

KAELEON intentionally uses a small set of structured stdout logs so Railway log volume and MongoDB storage stay bounded.

## Production setting

```env
LOG_LEVEL=INFO
LOG_STATE_REPEAT_SECONDS=300
LOG_REJECT_REPEAT_SECONDS=300
```

Do not use `DEBUG` continuously in production. It is only for short investigations.

## INFO events

- `REGIME_EVALUATED`: emitted only when the regime state changes, or after the repeat interval.
- `STRATEGY_EVALUATED`: emitted only when the selected strategy changes, or after the repeat interval.
- `SIGNAL_REJECTED`: one compact rejection per symbol/reason, throttled by the repeat interval.
- `RISK_REJECTED`: throttled compact risk rejection.
- `SIGNAL_ACCEPTED`: always emitted.
- `POSITION_OPENED`: always emitted with mode, symbol, side, entry, SL, TP, quantity and strategy.
- `POSITION_CLOSED`: always emitted with mode, symbol, exit reason, exit price and realized PnL.
- runtime/market/Telegram failures are emitted as `ERROR`.

Scanner details, every-decision traces, risk calculations, successful Telegram delivery, cooldowns and skipped entries are DEBUG-only.

## MongoDB policy

Operational logs are **not written to MongoDB**. The database stores only data required by the product:

- positions / orders;
- accepted trade decisions;
- the latest `user_engine_state` (upserted, not append-only);
- normal auth/billing/referral data.

Rejected/no-trade decisions are no longer appended to the `decisions` collection.

The `/user/activity` endpoint is a lightweight view synthesized from the current engine state plus existing positions. It does not need an events collection.
