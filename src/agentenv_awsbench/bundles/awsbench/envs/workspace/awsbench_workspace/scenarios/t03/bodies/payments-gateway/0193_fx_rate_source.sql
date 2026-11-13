-- ledger-writer 3.41.0 (PR #2217): record where an entry's FX rate came from. Nullable, no backfill.
ALTER TABLE ledger_entries ADD COLUMN fx_rate_source text NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS ledger_entries_fx_rate_source_idx
    ON ledger_entries (fx_rate_source) WHERE fx_rate_source IS NOT NULL;
