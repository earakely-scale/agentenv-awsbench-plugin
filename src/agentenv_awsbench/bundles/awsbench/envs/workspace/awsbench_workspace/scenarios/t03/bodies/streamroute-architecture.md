# streamroute architecture

streamroute consumes Kinesis `vl-payment-events` (240 shards; leases in DynamoDB `streamroute-leases`) and routes
each record to its ledger cell (`lc-01`..`lc-08`, 30 shards each) using a **shard map**. Business metrics
(bizmetrics: payment volume, auth success, ledger freshness, webhook lag) are computed from the events streamroute
routes.

## Process and threads
- One JVM per host (Java 17, user `streamroute`), plus the `sr-metrics-agent` sidecar (Go, same user, ~22 threads).
- Pools: route-worker **core 256 / max 384** (grows with load; at max above ~2,000 rec/s fleet ingest);
  cell-client 24 per cell (192); kinesis-consumer 64; netty event loop 32; JVM internal ~48; misc ~28.
- **PeerLink (ADR-007):** one long-lived TCP link to every other member, each with a dedicated inbound reader
  thread and a dedicated outbound writer thread: **2 platform threads per peer**. ADR-007: "simple; revisit above
  200 hosts."
- Shard-map builds start 8 worker threads (`shardmap/build_workers`) for ~40 s.

## Membership and shard maps
- Membership is gossiped. An existing host learns a new member within the discovery window (60 min, jittered);
  a new host starts from a 32-peer seed list. Members whose process stopped are dropped after 90 s.
- srcoord tracks running processes, host state (ROUTING, STALE, BROKEN, WARMING, EJECTED) and lease ownership.
- The shard map is built from `streamroute-config` (shard -> cell), cellmap cell health and peer gossip, rebuilt
  every 30 min, and usable for 45 min after its last successful build (STALE after a failed build, BROKEN once
  expired). A BROKEN host keeps its leases.
- Bootstrap sources (read at process start): `peers` (default, from ROUTING peers) or `authoritative` (cellmap
  BootstrapSnapshot + streamroute-config, ~90 s, full current srcoord membership). See RB-SR-07.

## Capacity model (2026-09, 120 hosts)
- ~62 rec/s per host routed; ledger-writer accepts up to ~6,500 rec/s.
- Minimum fleet for weekday peak with one AZ lost: **116 hosts**.
- Thread budget at max pool: ~770 non-peer threads per host (JVM + sidecar) + 2 per peer + 8 during a build.
  Headroom against the service limit was last measured in PLAT-1650 (96 hosts).
