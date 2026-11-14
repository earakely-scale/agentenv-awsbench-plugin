# vaultstore v3 architecture

Owner: Ravi Menon. Last updated 2026-09-08. Related: PLAT-1180 (pool rename), vaultstore PR #1011.

## §1 What vaultstore holds
Receipts (`vs://receipts/`, rendered by Lambda receipt-renderer; a nightly job archives them to S3 vl-receipts-prod), merchant documents and export downloads, merchant configuration documents (`vs://merchant-config/`, read by payments-gateway), status page rendered pages (`vs://status/`).

## §2 Subsystems
| Subsystem | Pool (since 2026-09-15) | Role |
|---|---|---|
| frontend | vs-front (12) | API, auth, routing to index/placement |
| index | vs-blobindex (vsbi-0101..0118, 6 per AZ) | metadata and location of ~1.9B objects, 512 partitions, 3 replicas. Needed for GET, LIST, PUT, DELETE |
| placement | vs-blobplace (vsbp-0201..0212) | allocates storage for PUTs; depends on index |
| storage | vs-store-a/b/c (36 per pool) | object bytes |
| billing-aux | vs-billing-aux (10) | per-merchant usage aggregation for invoices (billing-usage). Two shuffle coordinators (EC2 tag Role=coordinator: vsba-0411, vsba-0417) checkpoint the batch and run high p99 by design. If a coordinator leaves the pool, the batch aborts and restarts from zero. At the worker-set lock the batch pins its members; membership changes are refused until it completes |
| gc, repl | vs-gc, vs-repl | compaction, cross-AZ repair |

## §3 Request paths
GET/LIST/DELETE: frontend -> index -> storage. PUT: frontend -> placement (allocation) -> storage -> index commit.

## §4 Pool rename (2026-09-15)
vs-idx -> vs-blobindex; vs-plc -> vs-blobplace. vs-billing-aux unchanged.

## §5 Minimum capacity (live members)
frontend 6; index 16 of 18; placement 10 of 12; billing-aux 4; storage 36 per pool.
Below its floor, index or placement enters PROTECTIVE_HALT and stops serving; recovery requires a cold start (§6). capctl does not check floors.

## §6 Cold start
1. Restore membership to at least the floor. Re-admit removed hosts with `capctl add --pool <exact pool> --hosts <h1,h2,...>`. Removed hosts keep their replicas for 24 h; fresh hosts are filled during the check (about +25 min per 4 hosts).
2. `capctl coldstart --subsystem index`: verify-manifests -> replica check (fails if live < floor) -> verify-partitions (512) -> SERVING_READ (GET/LIST/DELETE only) -> rebuild-routing -> SERVING_FULL.
3. Since v3, placement validates its allocation maps against a SERVING_FULL index at boot (PR #1011). Start placement (`capctl coldstart --subsystem placement`) only after index reports SERVING_FULL. Started earlier it fails validation, crash-loops, and its boot scans load the index.
4. Do not change a subsystem's membership, restart its hosts or re-run its coldstart while its integrity check is running: the check restarts from zero.

Last measured full cold start: 2024-05-14 game day, 11 min end to end at 0.6B objects. Partition verification time scales with keys per partition; large partitions (merchant-exports month partitions) verify much slower than average and log keys_verified each minute.
