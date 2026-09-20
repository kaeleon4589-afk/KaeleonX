# KAELEON Motor Finalization v0.6

The trading engine is now functionally frozen before the account platform is built.

## Final safeguards
- One open position per symbol.
- Per-symbol/timeframe decision cooldown.
- In-flight decision guard prevents concurrent duplicate submissions.
- Every decision keeps the same `decision_id` through regime, intent, risk and execution.
- Position manager persists state after every mark/TP1/TP2/SL transition.
- Reconciliation reports local/exchange mismatches without silently overwriting state.
- Paper mode remains isolated from live execution.
- Live execution remains disabled until private-order reconciliation is explicitly validated.

## TP/SL invariant
- Structural stop plus volatility buffer.
- TP1 at 1R and 50% reduction.
- Stop moves to entry after TP1.
- TP2 defaults to 2R with a configurable minimum of 1.8R.
- No fixed-percent stop replaces structural invalidation.

## Frozen scope
No new strategy, indicator or filter is being added in this phase. Further changes require a deliberate versioned strategy decision.

## Next product layer
Before coding the frontend, define and approve the complete account/authentication contract: registration, email verification, login/session lifecycle, password recovery, roles, plans, permissions, CoinW credential vaulting, audit and account states.
