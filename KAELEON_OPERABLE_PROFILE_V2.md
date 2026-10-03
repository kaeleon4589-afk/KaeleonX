# KAELEON — Operable Profile V2

## Objective
Move Engine V2 from an over-selective validation profile to an **operable baseline**: keep structural quality controls, SHOCK/DEAD protection, dynamic targets, invalidation, anti-chase and execution safety, while allowing acceptable (not only near-perfect) Breakout/Retest and Liquidity Sweep setups to reach execution.

This profile does **not** impose a daily trade quota and does not claim profitability. It is intended to generate enough real samples to calibrate quality from observed trades.

## Evidence from the supplied log
The accumulated funnel showed roughly 2.8k analyzed evaluations per user, only 16–17 ARMED, 7–8 TRIGGERED and 3–4 FILLED. The dominant aggregate reason was `no_watchable_precursor`; after trigger, the main blocker was `stop_inside_market_noise`.

## Changes

### Regime V2
- TREND score default: `61 -> 58`.
- RANGE score default: `58 -> 54`.
- Trend behavior accepts strong ADX + low choppiness even when efficiency is still developing.
- TRANSITION remains blocked unless it is a clearly developing trend/range; such soft routes run with reduced risk multiplier (`0.75` trend, `0.70` range).
- SHOCK and DEAD remain hard blocked.

### Breakout/Retest V2
- Breakout minimum body: `0.42 -> 0.34`.
- Breakout RVOL floor: `0.80 -> 0.65`.
- Close-position requirement relaxed moderately.
- Breakout age default: `6 -> 8` 5m bars.
- Retest tolerance: `0.28 -> 0.38 ATR`.
- Invalidation buffer: `0.48 -> 0.62 ATR`.
- Continuation confirmation can be smaller (`body >= 0.16`).
- Confirmation freshness: up to 3 bars.
- Anti-late-entry extension: `0.95 -> 1.15 ATR`.
- Quality floor default: `68 -> 62`.
- HTF alignment now allows one timeframe to be neutral/weakly different; a strong opposite HTF still rejects.

### Liquidity Sweep V2
- Sweep age default: `4 -> 6` 5m bars.
- Wick threshold: `0.30 -> 0.24`.
- RVOL floor: `0.75 -> 0.60`.
- Edge watch distance: `0.38 -> 0.55 ATR`.
- Reclaim/confirmation thresholds relaxed moderately.
- Confirmation freshness: up to 3 bars.
- Reclaim-loss tolerance: `0.15 -> 0.22 ATR`.
- Quality floor default: `68 -> 62`.
- Dynamic target logic remains unchanged.

### ARMED -> TRIGGERED
- Severe order-book conflict threshold: `0.45 -> 0.55`.
- Live micro-confirm quality: `82 -> 76`.
- Live trigger/zone tolerances widened moderately.
- Closed 1m confirmation body: `0.18 -> 0.14`.
- Dynamic RR and structural TP remain mandatory.

### Final execution guard
For Engine V2 only:
- final market-noise stop floor: `0.22 ATR` (legacy remains unchanged);
- spread floor: `2x spread` (legacy remains unchanged);
- final order-book veto only at imbalance `>= 0.50`;
- final chase/adverse tolerances do not duplicate stricter legacy thresholds.

## Protections deliberately kept
- SHOCK / DEAD hard block.
- Invalid structure / invalid stop geometry rejection.
- Target reached before entry cancellation.
- Dynamic structural TP.
- Dynamic minimum viable RR.
- Anti-chase lifecycle.
- Order-book severe-conflict veto.
- Real-time TP/SL exit monitor.

## Railway values for this profile
If these variables already exist in Railway, update them to:

```env
V2_REGIME_TREND_SCORE_MIN=58
V2_REGIME_RANGE_SCORE_MIN=54
V2_BREAKOUT_MIN_SCORE=62
V2_BREAKOUT_MAX_AGE_BARS=8
V2_SWEEP_MIN_SCORE=62
V2_SWEEP_MAX_AGE_BARS=6
V2_TRIGGER_ORDERBOOK_CONFLICT=0.55
```

Optional live-confirm variables (only set them if they already exist or you want explicit server values):

```env
V2_LIVE_CONFIRM_MIN_QUALITY=76
V2_LIVE_CONFIRM_MAX_QUOTE_AGE_MS=2500
V2_LIVE_CONFIRM_TRIGGER_TOLERANCE_ATR=0.05
V2_LIVE_CONFIRM_ZONE_TOLERANCE_ATR=0.30
```

Do not reintroduce fixed target RR variables.

## Verification
- `python -m compileall -q app` — PASS
- `pytest -q tests` — 319 passed
