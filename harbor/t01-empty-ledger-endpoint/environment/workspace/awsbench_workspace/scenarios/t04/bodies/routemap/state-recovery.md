# Routemap state recovery

Owner: Platform (routemap). Applies to partial corruption or loss of items in `routemap-state`.
For total regional loss use the DR runbook (repoint `/routemap/prod/state_table`), see POST-2026-006.

## 1. Contain
- Pause the mutating workflows: `rmctl workflows pause scaler` and `rmctl workflows pause rebalancer`
  (or disable rules `routemap-scaler-tick` / `routemap-rebalancer-tick`, or set
  `/routemap/prod/workflows/<name>/enabled=false`). The reconciler is reconcile-only and can stay on.
- If routemap-api is turning bad state into publishes, set `rmctl api set update_route=off`.

## 2. Find the bad write
- `rmctl audit tail` (or `/routemap/prod/audit` in CloudWatch Logs). Note the first write time of the bad job.

## 3. Restore into a NEW table
- Point-in-time restore of `routemap-state` into a new table, just before the bad write. Never restore
  over `routemap-state` and never repoint `/routemap/prod/state_table` for partial corruption.
- Takes about 15-20 minutes. The DynamoDB stream (NEW_AND_OLD_IMAGES, 24 h) also holds the old images.

## 4. Identify the affected keys
- Compare the restored table with the live one (`rmctl state diff --source <table> --out <dir>`).
- Items written legitimately after the bad write must keep their current values.

## 5. Merge
- `rmctl state merge --source <table> --keys <file|ids> (--whole-item|--attributes a,b) [--dry-run]`
- Merge writes `route_version = max(live, source) + 1` and `publish_mode = SUPPRESS`.

## 6. Publish
- `rmctl publish --keys <file>`. The data plane accepts only versions above the loaded one.

## 7. Verify
- `rmctl dataplane status --keys <file>` shows OK for every key; payments-gateway errors for those merchants stop.

## 8. Resume gradually
- `rmctl workflows resume scaler --canary 10`, watch one tick, then `rmctl workflows resume scaler`
  and `rmctl workflows resume rebalancer`.

## Never
- Never delete and recreate a live route: `route_id` and `idem_ns` regenerate and client retries stop
  deduplicating (duplicate captures).
