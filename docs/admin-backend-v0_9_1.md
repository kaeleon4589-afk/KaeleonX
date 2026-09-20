# KAELEON Admin Backend v0.9.1

## Access model
- USER sees only user-scoped platform data through user endpoints.
- ADMIN access is granted by the authenticated session phone matching `ADMIN_PHONE` from environment configuration.
- The frontend cannot grant admin access; authorization is enforced server-side on every `/admin/*` route.
- `password_hash` is never returned by admin endpoints.
- Admin access grants are denied when `ADMIN_PHONE` is missing.

## Admin API surface
- `GET /admin/me`
- `GET /admin/dashboard`
- `GET /admin/users`
- `GET /admin/payments`
- `GET /admin/trading/positions`
- `GET /admin/trading/decisions`
- `GET /admin/system/events`
- `GET /admin/system/config`

The API is intentionally backend-first. Visual/admin frontend work is not included in this step so later admin requirements can be added without locking the UI prematurely.

## Environment
```env
ADMIN_PHONE=+1XXXXXXXXXX
```
The configured phone should be stored normalized in E.164 form, matching the authenticated account phone.

## Manual LIVE activation / user access control (v0.9.2)
- Admin can locate a verified account primarily by normalized phone; Telegram user ID is supported as an alternate lookup.
- `POST /admin/users/live-days` adds an arbitrary number of LIVE access days cumulatively. If existing access is expired, the new period starts immediately; otherwise it extends the current expiration.
- `POST /admin/users/ban` supports an optional duration in days. A timed ban uses `suspended` plus `ban_until`; a permanent administrative block uses `blocked`.
- Timed bans automatically become `active` when the user next authenticates after `ban_until`.
- `POST /admin/users/unban` restores an account to `active` immediately.
- `DELETE /admin/users` performs reversible logical deletion (`deleted`) rather than destroying audit/user records, because the product requires an admin restore path.
- `POST /admin/users/restore` restores a logically deleted account to `active`.
- All these actions revoke active sessions when access is restricted and create audit events.
- Password hashes and credentials remain excluded from admin responses.
