# KAELEON TP/SL design

## Stop Loss
SL is not a fixed percentage.
- Liquidity Sweep: the real sweep extreme plus ATR buffer.
- Breakout + Retest: the retest/invalidation structural area plus ATR buffer.
- The Risk Engine rejects invalid or excessively large structural risk.

## Take Profit
- TP1 = 1R by default.
- TP2 = at least 1.8R, default 2R.
- A valid nearby market/liquidity barrier may constrain a target only when it still satisfies the minimum RR requirement.
- TP1 closes 50% of the remaining position and moves the stop to breakeven.
- TP2 closes the remainder.
- A stop hit closes the remaining position.

## Why
This preserves the previously agreed principle: SL follows market structure and volatility, while TP is RR-based and contextual rather than a fixed percentage.

## CoinW live mapping
CoinW Futures supports SL/TP fields on order placement and dedicated SL/TP order types. An accepted order ID does not mean the order was filled; fills must be confirmed through order/position state. Live mapping will therefore be implemented only after authentication and private execution flows are verified end-to-end.
