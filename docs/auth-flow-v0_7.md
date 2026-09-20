# KAELEON Authentication v0.7

## Registration
Phone + password -> Telegram bot -> explicit Telegram contact share -> return to KAELEON -> Verify -> exact normalized phone match -> account active.

Password fields support show/hide in the future UI. Backend never stores plaintext passwords.

## Login
Phone + password -> authenticated session. **No Telegram verification on login.**

## Account states
`pending_verification`, `active`, `blocked`, `suspended`.

## Roles/plans
Roles: `USER`, `ADMIN`. Default plan: `FREE`.

## Security
- Phone numbers normalized before comparison.
- Passwords hashed with scrypt.
- Sessions use opaque random bearer tokens; only their SHA-256 hashes are persisted.
- Verification is decided server-side.
- MongoDB Atlas indexes enforce unique phone/user/session/challenge identities.
- Telegram is registration verification only.
