# KAELEON ADMIN DATA VIEWS v0.10.5

Admin visual views now have a reusable authenticated data layer for:
Users, LIVE subscriptions, Payments, Referrals, Operations, Engine, Markets and Audit.

The UI calls `/admin/*` only after the existing `adminFetch` authorization-aware wrapper.
The backend remains the source of truth. This package does not fabricate operational values.
