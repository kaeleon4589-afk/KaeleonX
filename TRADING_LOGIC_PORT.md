# Trading-X Hyper Pro -> KAELEON / CoinW logic port

This build ports the executable decision logic from the supplied Trading-X Hyper Pro repository while retaining CoinW as the data/execution venue and KAELEON's per-user risk/account model.

## Market scanner

The CoinW scanner now uses the source ranking formula adapted to CoinW ticker fields:

- 50% 24h notional-volume score,
- 30% open-interest score (source fallback `0.3` when CoinW does not provide OI),
- 20% directional 24h trend score.

The shortlist blocks the source high-noise/meme universe plus the strategy-level exclusions. Normal scanner caching remains short (`MARKET_SCANNER_CACHE_SECONDS`, default 30s); if CoinW temporarily fails or returns no usable universe, the last known-good shortlist can be reused for up to 300s, matching the source bot's fail-safe behavior. The KAELEON coordinator already rotates through the shortlist, so a second independent source round-robin layer is not needed.

## Regime detector

Feature extraction and classification now follow the source calibrated router:

- RMA ATR and ADX,
- choppiness,
- efficiency ratio,
- realized volatility,
- wick instability and body quality,
- breakout-failure ratio,
- EMA 20/50/200 stack and slopes,
- rolling VWAP distance in ATR,
- recent 3/6-bar movement,
- BTC ATR/realized-vol/recent-move shock ratio.

Regimes are `TREND_CONTINUATION`, `VOLATILE_SWEEP`, `RANGE`, and `UNKNOWN`. Source thresholds, ready/decisive rules, confidence formulas and priority order are retained. The state machine preserves 3-bar confirmation, 2-bar cooldown and 3-bar minimum active duration by default.

The executable router now also follows the source production defaults: `TREND_CONTINUATION` maps to Breakout+Retest, `VOLATILE_SWEEP` maps to Liquidity Sweep, while `RANGE` and `UNKNOWN` do not place orders. The source repository keeps range mean-reversion in shadow/observation mode. The optional trend liquidity probe is supported through `STRATEGY_ROUTER_LIQUIDITY_PROBE_ENABLED`, but remains disabled by default exactly as in the supplied source.

## Breakout + Retest

The source multi-timeframe continuation logic is used with:

- 1h directional/bias context,
- 15m confirmation,
- 5m reset/retest/trigger,
- EMA 20/50/200,
- H1 ADX >= 12, M15 >= 11, M5 >= 9.5,
- ATR% 0.075% to 1.80%,
- source reset windows/tolerances/extensions,
- minimum 260 bars and 92% non-zero-volume quality,
- source score `69 + 28 * quality`, capped at 100,
- source-calibrated fixed SL/TP bands and RR-target formula.

## Liquidity Sweep Reversal

The port preserves the source lookback/age, sweep depth, wick quality, relative-volume trigger, EMA recovery, body/close-position checks, extension guard, post-sweep invalidation, structural RR gate, score weights, and fixed SL/TP formula. Source partial TP is disabled, so KAELEON does not impose its older forced 50% TP1 behavior on these strategies.

`structural_rr_estimate` remains a setup-quality measurement where the source strategy defines one. `execution_rr` is always recomputed from the final Entry/SL/TP and is what is persisted/notified for the actual trade geometry.

## Shared DEMO/LIVE decision path

DEMO and LIVE run through the same CoinW market snapshots, scanner, regime detector, strategy router, geometry checks and risk engine. Only execution differs:

- DEMO: simulated fill/capital, live CoinW data.
- LIVE: CoinW order submission, confirmation and exchange reconciliation.

Hyperliquid-specific wallet/order transport, DEX fees and owner accounting are intentionally not copied.
