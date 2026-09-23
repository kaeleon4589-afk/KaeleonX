# KAELEON core pipeline refactor

This build replaces the patch-by-patch trading path with an explicit staged pipeline:

`Market -> Regime -> Strategy -> Signal -> Risk -> Execution -> Position -> Persistence -> Notification`

## Root causes corrected

1. **DEMO fill crashed before `POSITION_OPENED`.** `PaperExecutionEngine.submit()` returns `mode=demo`. The orchestrator also passed `mode=` explicitly while expanding the execution result into the audit event. Python raised a duplicate-keyword `TypeError` immediately after a successful paper fill. The execution payload is now sanitized before logging and a regression test proves a DEMO fill reaches `POSITION_OPENED`.
2. **Mongo/BSON runtime objects leaked into documents.** Position documents could contain `Enum`/dataclass values and order results could contain a live `Position` object. All persistence now crosses a BSON-safe serialization boundary; the runtime `position` object is never stored inside an order document.
3. **Synchronous Mongo work sat on the market/event loop.** Critical trade persistence now runs via `asyncio.to_thread` with timeout/retry bounds. Diagnostic decisions happen only after the execution outcome is known. Engine-state writes are also moved off the event loop and cadence-limited.
4. **Engine state wrote too often.** The previous state signature included the rotating market symbol, allowing each scanner tick to bypass the intended throttle. State/equity persistence is now bounded by `ENGINE_STATE_PERSIST_SECONDS` unless a real position state change forces an update.
5. **LIVE reconciliation was not symbol-scoped.** A sync of one CoinW symbol could be compared with local positions from another symbol. Reconciliation now scopes exchange and local positions to the requested symbol.
6. **Delayed LIVE fills had no coherent terminal path.** Exchange-accepted orders that are not immediately visible are `EXECUTION_PENDING`. The runtime is locked against a second entry while CoinW confirms; later reconciliation promotes the exchange position, persists it and sends the open notification.
7. **DEMO equity reset after restart.** DEMO equity is reconstructed from persisted realized PnL/fees instead of blindly returning to the initial wallet value.
8. **Telegram/database side effects could interfere with trading.** Telegram user lookup is moved off the event loop and database failures after a real fill cannot relabel the trade as rejected.

## Pipeline contract

After `SIGNAL_ACCEPTED`, the signal must resolve visibly as one of:

- `RISK_REJECTED`
- `EXECUTION_REJECTED`
- `EXECUTION_PENDING` (LIVE exchange accepted, confirmation still pending)
- `POSITION_OPENED`
- `PIPELINE_ERROR`

A confirmed fill is inserted into in-memory position state immediately to prevent duplicates. The position is then persisted with bounded retries, `POSITION_OPENED` is emitted, and Telegram is notified. Persistence failure is reported separately and retried; it does not convert an actual fill into an execution rejection.

## LIVE reconciliation

Every LIVE snapshot first synchronizes the same CoinW symbol. This allows KAELEON to recover delayed fills and exchange-side closes without mixing symbols. Recovered opens/closes are persisted and trigger the same Telegram notification path as immediate fills.

## Production persistence policy

Operational audit logs stay on stdout/Railway only. MongoDB keeps product state: positions, orders, accepted decisions, engine state, users, billing/referrals/sessions. Engine state is cadence-limited and trade persistence has explicit timeouts/retries.

Defaults:

- `ENGINE_STATE_PERSIST_SECONDS=15`
- `TRADE_PERSIST_TIMEOUT_SECONDS=4`
- `TRADE_PERSIST_RETRIES=2`
- `MARKET_SCANNER_FAILSAFE_SECONDS=300`

These variables are optional; the defaults are built into the application.
