# ledger-writer changelog

## 3.41.0 (2026-11-16)
- New nullable column `fx_rate_source` on `ledger_entries`, with a partial index (migration 0193). Existing writes are
  unchanged; the column is filled from 3.42.

## 3.40.2 (2026-10-29)
- Retry budget for cell acks raised from 2 to 3.
