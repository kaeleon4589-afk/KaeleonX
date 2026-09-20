# KAELEON Strategy + Execution Build v0.2

The strategy layer has been moved from specification-only to executable logic.

## Strategy behavior
- Breakout + Retest: structural level -> acceptance -> retest -> confirmation -> intent.
- Liquidity Sweep: liquidity raid -> reclaim -> displacement -> intent.
- Neither strategy is forced to trade.
- The router can return `None` / NO_TRADE.
- Quality is scored from multiple evidence sources rather than requiring every signal to be perfect.

## Execution behavior
- Paper execution fills virtually at the strategy entry price while using live market data upstream.
- Live execution is isolated behind `CoinWExecutor`.
- A CoinW order response is treated as request acceptance, not a fill; fill/position confirmation is a separate lifecycle step.
- `decision_id` is passed into every order intent for traceability.

## Not implemented yet
- Exact CoinW authentication/signature production code.
- Exchange-specific contract rounding/min-size validation from the live instrument cache.
- Full WebSocket reconnection/state reconciliation.
- Production database schema for orders/positions/PnL.

These are deliberately isolated so strategy logic does not depend on exchange details.
