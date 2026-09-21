# Trading-X -> KaeleonX trading logic port

This build ports the executable trading decision model from Trading-X Hyper Pro into KaeleonX while keeping CoinW as the market/execution venue.

Implemented runtime path:

1. CoinW all-instrument ticker scanner ranks markets and blocks configured meme/high-noise symbols.
2. Shortlisted markets are rotated and analyzed with 5m, 15m and 1h candles plus live order book.
3. Regime features include ADX, choppiness, efficiency ratio, EMA-stack alignment, wick instability, body quality, breakout-failure ratio, ATR%, recent move and BTC shock ratio.
4. Regime states: TREND_CONTINUATION, VOLATILE_SWEEP, RANGE, UNKNOWN, with 3-bar confirmation, 2-bar cooldown and 3-bar minimum active duration.
5. TREND routes to the MTF breakout/reset/retest continuation strategy using 1h bias + 15m confirmation + 5m reset/trigger.
6. VOLATILE/RANGE routes to liquidity-sweep reversal. A high-quality liquidity setup may be probed during a trend, matching the source router behavior.
7. Strategy-derived SL/TP are fixed-price exits with source-calibrated percentage bands. Source strategies have partial TP disabled; KaeleonX therefore closes at the fixed target rather than forcing its former 50% TP1 behavior.
8. Existing KaeleonX RiskManager still caps notional by user equity, risk_per_trade, leverage and margin fraction.
9. DEMO and LIVE continue through the same signal/regime/risk path; only execution differs.

Exchange-specific Hyperliquid wallet/order code, owner fees and DEX-specific accounting were intentionally not ported.
