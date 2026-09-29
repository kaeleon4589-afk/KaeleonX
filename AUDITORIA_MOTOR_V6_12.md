# KAELEON v6.12 — Trigger lifecycle recovery

## Evidence from production logs

After the v6.11 lifecycle-gap deploy the engine progressed beyond WATCHING and reached ARMED, but the cumulative rejection funnel still showed zero TRIGGERED/FILLED setups. At ~2,000 analyzed decisions per user it reported 16 ARMED, 16 ARMED cancellations, with 9 `setup_invalidated`, 5 `trigger_rr_too_low`, 1 `setup_chased`, 1 `trigger_invalid_geometry`, and 2 `waiting_trigger` observations.

The `setup_invalidated` cases remain hard cancellations. Example: USELESS armed LONG with stop/invalidation ~0.2400509 and was later observed at ~0.23986, below its stop. That setup was correctly rejected.

## Root cause corrected

The arm-time target is already front-run and RR-capped relative to the arm-time trigger. At trigger time, the engine previously reused that stale target against a newer executable quote. When price advanced toward the target while the engine waited for micro-confirmation, remaining RR could fall below `ARM_MIN_RR=1.10`, causing a permanent consume (`trigger_rr_too_low`).

This is inconsistent because the original structural target is preserved in setup metadata and can support a recalculated executable TP from the actual entry quote.

## Changes

1. Rebase TP from `structural_target_price` at trigger time using the actual executable quote.
2. Reapply the strategy RR cap from the actual executable quote.
3. Keep `trigger_rr_temporarily_low` as PENDING instead of consuming the setup. A retrace can restore RR before TTL/invalidation.
4. Keep true structural invalidation (`setup_invalidated`), chase protection, expiry, trend flip and genuinely invalid geometry as terminal cancellations.

## Intentionally unchanged

- `ARM_MIN_RR = 1.10`
- Breakout RR target cap = environment/configured value (currently 1.50)
- Liquidity Sweep RR target cap = environment/configured value (currently 1.30)
- Stop/invalidation rules
- 1m confirmation quality
- anti-chase guard
- dynamic protection setting

The change removes a one-way lifecycle trap; it does not force trades through invalid setups.
