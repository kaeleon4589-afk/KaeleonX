# KAELEON v6.13 — Consistent ARMED trend context

## Root cause confirmed after v6.12 deploy

Post-deploy logs showed ZEST LIQUIDITY_SWEEP LONG armed with regime `trend_bias=long`, `ema_stack_alignment=0.75`, `ema_alignment_edge=0.50`, ADX ~26.188. Roughly 20 seconds later, before a new closed 5m bar existed, ARMED revalidation reported `trend_bias=neutral`, alignment 0.50 and edge 0.0, cancelling the setup with `trend_direction_lost`.

The discovery snapshot fetched 321 5m klines (about 320 closed bars), while the ARMED monitor fetched only 261 (about 260 closed bars). `regime_features()` computes EMA20/50/200 from the supplied series and `ema()` seeds from the first element, so changing the history depth can change EMA200/alignment even for the same final closed candle.

## Fix

`MarketCoordinator._armed_trend_candles()` now requests 321 5m klines, matching discovery. The 15-second per-symbol cache remains unchanged, so polling frequency/API pressure is not materially increased; only the payload depth of the cached 5m refresh changes.

No strategy threshold, RR, stop, anti-chase, retest geometry, 1m confirmation, or true trend-flip protection was relaxed. A real neutral/opposite trend on a newly closed 5m candle can still cancel the setup.

## Files changed

- `app/market/coordinator.py`
- `tests/test_armed_priority_monitor.py`
- `AUDITORIA_MOTOR_V6_13.md` (new)
