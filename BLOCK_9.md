# KAELEON Block 9 — LIVE billing + referrals integration

## Commercial rules
- DEMO: unlimited.
- LIVE trial: 5 days, starts only on first LIVE activation.
- Paid LIVE: 15 days / 5 USDT; 30 days / 10 USDT.
- Renewals are cumulative from the current LIVE expiry.
- Payment network: BNB Smart Chain.
- TX hash is independently verified before activation.
- Payment order expires after 30 minutes.
- A confirmed TX hash cannot be reused.

## Referral rules
- Every account receives a unique referral code.
- A referral may be attached at registration.
- Self-referral is rejected.
- No reward for registration or DEMO.
- First confirmed paid LIVE plan only generates a reward.
- 15-day plan -> 7 LIVE days for referrer.
- 30-day plan -> 15 LIVE days for referrer.
- Renewals of the referred user do not generate additional reward.
- Referral reward extends the same `subscription_expires_at` entitlement used by billing.

## Configuration
`PAYMENT_WALLET`, `PAYMENT_NETWORK`, `BSC_RPC_URL`, `USDT_BSC_CONTRACT`, and `USDT_DECIMALS` remain configuration values. No token contract address is hard-coded.

## Security
The server verifies the BNB Smart Chain chain ID, receipt success, configured USDT contract, destination wallet, and exact token amount before activation.
