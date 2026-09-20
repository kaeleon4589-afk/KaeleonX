# KAELEON — User Integration v0.10.1

## Unified flow
Registration → Telegram verification → Login → Tutorial → Dashboard → Execution → Operations → Performance → Settings.

## Isolation
Authenticated user endpoints resolve the session to `user_id` and scope user-facing trading data by that owner. Decision lookup requires both `decision_id` and authenticated `user_id`.

The orchestrator accepts `user_id` and persists it on decisions, positions and orders, preserving ownership through the decision lifecycle.

## Single source of truth
Dashboard, Execution, Operations and Performance read the same persistence layer. No duplicated client-side trading state is introduced.

## Platform-owned controls
The user API explicitly exposes markets and timeframes as informational platform-managed state:
- Markets: selected automatically by KAELEON.
- Timeframes: internal MTF configuration per strategy.
- Leverage: fixed internally.

## Session
Logout revokes the current session. Subsequent authenticated requests are rejected.

## Scope
This block integrates the authenticated user flow and backend data contracts. It does not activate LIVE execution, alter strategy logic, or expose technical engine controls to users.
