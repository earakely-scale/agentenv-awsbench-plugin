# services/ledger-writer

Synchronous ledger API behind `ledger.internal.vantleaf.io` (Envoy mTLS ingress `vl-ledger-ingress-asg`). Runs 12 tasks on
`cp-ledger` (`vl-ledgerwriter-asg`, lw-a/b/c-01..04, one task per host). DB pool 25 per task (`DB_POOL_SIZE`), Aurora
`vl-ledger-db` max_connections 400.

- Admission control: `/vl/prod/ledger-writer/admission/enabled`; quotas `/vl/prod/ledger-writer/admission/quota/<caller>`
  (req/s; rejected requests get 429 Retry-After: 1).
- PAY-655: queries are not cancelled when the client times out; a pool can stay full of abandoned work.
