# KAELEON ADMIN v0.10.4 — Backend Integration

The admin UI now uses authenticated requests against `/admin/dashboard`.
The API contract documents the remaining admin data routes.

Security:
- Backend authorization is authoritative.
- 401/403 is surfaced as an admin access state.
- Credentials are sent with requests for session-based auth.
- No secrets or password hashes are rendered.
- Sensitive mutations must remain server-side and auditable.

This block does not alter trading logic, market selection, timeframe selection, leverage policy, or USER permissions.
