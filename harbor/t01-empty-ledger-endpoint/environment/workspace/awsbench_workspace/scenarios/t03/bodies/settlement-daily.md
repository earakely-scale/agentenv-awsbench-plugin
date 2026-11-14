# settlement-daily and the 12:30 readiness gate (FIN-880)

- `settlement-daily` runs at **13:00 ET** on business days and builds next-day payouts for ~4,000 merchants from
  the ledger.
- **Readiness gate at 12:30 ET:** ledger lag < 300 s on all eight cells (`srctl lag` or
  `Vantleaf/Streamroute CellLagSeconds`).
- If not ready, the run is held; payouts slip one business day and fin-bot opens a "Settlement held" FIN issue.
- Questions: Marco Ferri (payments).
