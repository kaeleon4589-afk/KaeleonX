# CoinW order-book execution fix

## Verified symptom

`SIGNAL_ACCEPTED` for MARSCOIN was followed by `EXECUTION_REJECTED`
`reason=market_unavailable`, `orderbook_valid=false`. The final core pipeline
correctly rejected execution because it had no bid and ask. The root cause of
that empty order book was inside `app/market/coordinator.py`.

## Verified root cause

CoinW documents REST `/v1/perpumPublic/depth` levels as
`{"p": price, "m": quantity}`. The KAELEON decoder read only the generic keys
`{"price": ..., "quantity": ...}`. Every documented CoinW depth level was
silently discarded, yielding `bid=None`, `ask=None`, `orderbook_valid=false`.

Official API docs:
https://www.coinw.com/api-doc/en/futures-trading/market/get-order-book-of-an-instrument

## Changes

- `app/market/coordinator.py`: decode CoinW `p`/`m`, retain generic aliases,
  reject invalid prices/sizes, select best bid/ask even when unsorted, and reject
  empty or crossed books.
- `app/orchestrator.py`: a strategy setup without executable quotes is now
  `MARKET_DATA_SKIPPED` before `SIGNAL_ACCEPTED`. The existing post-risk guard
  remains as a second protective boundary. Never invent bid/ask from candles.
- `app/logging/logger.py`: expose `MARKET_DATA_SKIPPED` at INFO but throttle
  repeated identical failures to avoid Railway logging costs.
- `tests/test_coinw_depth_pipeline.py`: regression tests for the CoinW wire
  format, quote validation, and DEMO signal -> position persistence -> callback.

## Verification and limitations

- All tests in this repository passed locally: 87 passed (including 7 new tests).
- Python bytecode compilation passed.
- This does **not** imply a Railway or real CoinW live test has been performed.
- Deploy only the backend/embedded worker. No environment changes required.
- Invalid order books remain fail-closed in both DEMO and LIVE; those setups do
  not create accepted signals until CoinW provides valid two-sided quotes.
