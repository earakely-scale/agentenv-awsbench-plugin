# streamroute

Routes `vl-payment-events` records to ledger cells. Runs as user `streamroute` under systemd unit `streamroute`.

## Operations (srctl)
- `srctl fleet status` - members and host states from srcoord (ROUTING, STALE, BROKEN, WARMING, EJECTED)
- `srctl host status` - this host's shard map (state, epoch, last success, last error)
- `srctl lag` - per-cell backlog and lag
- `srctl leases --summary`, `srctl membership`, `srctl config`
- `srctl restart` - runs `sr-apply-limits` (renders the systemd limits drop-in from SSM `/streamroute/prod/os/limit_nproc`, then `systemctl daemon-reload`) and restarts the unit. Used by `fleet_restart`.

## Configuration (SSM `/streamroute/prod/*`, read at process start)
- `shardmap/bootstrap_source`: `peers` (default) or `authoritative` (cellmap BootstrapSnapshot + streamroute-config). Use `authoritative` when few hosts are ROUTING (RB-SR-07).
- `shardmap/rebuild_interval` 30m, `shardmap/ttl` 45m, `shardmap/build_workers` 8
- `health/warmup_grace_seconds` 600 (srcoord ejects hosts not ROUTING in time; 15 min quarantine)
- `jvm/xmx`, `jvm/opts`
- `os/limit_nproc` - PLAT-HARD-03 baseline, changed only through a tested change record (Peak Freeze §4.3)
