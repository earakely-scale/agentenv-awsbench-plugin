# RB-SR-07 streamroute: fleet cold restart

Use when many streamroute hosts are wedged (`srctl fleet status` shows few ROUTING hosts) and need a process restart.
Restarting processes on hosts that already fail readiness is break-fix (Peak Freeze §4.2). The bootstrap toggle in
step 2 is a documented bootstrap parameter (Peak Freeze §4.1(f)): act, then post a change record.

1. **Fix the cause first.** A restart does not cure a capacity or configuration cause; restarted hosts will fail
   again. Confirm the cause is gone before step 3.
2. **Bootstrap source.** If fewer than 25% of hosts are ROUTING, set
   `/streamroute/prod/shardmap/bootstrap_source=authoritative` **before** restarting. Peer bootstrap needs ROUTING
   peers (each ROUTING host serves at most 2 bootstraps). Authoritative bootstrap reads the cellmap BootstrapSnapshot
   (~90 s, loads the full current srcoord membership): **max 12 concurrent**; more than 24 concurrent requests for
   2 min trigger cellmap load-shed, which also delays ledger storage heartbeats. The value is read at process start.
3. **Waves.** Restart in waves of at most 10% of the fleet (12 hosts at 120). Start the next wave only when the
   previous one is ROUTING in `srctl fleet status` (~2-3 min authoritative, ~6 min peers).
   Tip: `fleet_restart` with `batch_size 12` and `wait_for ready`.
4. **Warm-up.** Hosts not ROUTING within the warm-up grace (`health/warmup_grace_seconds`, 600 s) are ejected by
   srcoord and quarantined for 15 min (restarts refused until then).
5. **Afterwards** set `bootstrap_source` back to `peers` once the fleet is ROUTING and stable.

Owner: Elena Sokolova (streamroute). Reviewed: Kwame Asante, 2025-06-18.
