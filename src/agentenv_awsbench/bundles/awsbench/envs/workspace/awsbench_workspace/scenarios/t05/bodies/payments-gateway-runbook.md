# payments-gateway runbook

Owner: Payments (Hannah Becker).

## §1 Dependencies
Merchant configuration documents are read from vaultstore (`vs://merchant-config/<mch>/vNN.json`) and cached in Redis with a 20-minute soft TTL. The top 500 merchants by volume also have a last-known-good snapshot in memory; long-tail merchants do not.

## §2 Error codes
PG-E-CFG-503: merchant config fetch failed (vaultstore unavailable) and no usable cached copy; the authorization is declined.
PG-E-3DS-*: 3DS challenge handling (checkout-api side).

## §3 Dashboards
Vantleaf/Payments AuthErrorRate, AuthRequests; per-code breakdown in Logs Insights on /vl/prod/payments-gateway.

## §4 vaultstore unavailable
If vaultstore is unavailable for more than 10 minutes, use one documented fail-safe toggle (a Peak Freeze §4.1(f) standing mitigation during a declared incident: act, then post the change record within 15 minutes):
- `/vl/prod/payments-gateway/config-cache/serve-stale = true`: serve soft-expired cache entries for up to 24 h (preferred), or
- `/vl/prod/payments-gateway/config-source = s3-snapshot`: read the nightly snapshot in `s3://vl-merchant-exports/config-snapshots/<date>/`.
The gateway polls these parameters every 30 s. Revert after vaultstore is fully serving (that revert is a normal change: IC approval).
