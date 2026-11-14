# LEDG-RB-2 ledger-writer: elevated latency or 503s

ledger-writer (ECS service on capacity provider `cp-ledger`, ASG `vl-ledgerwriter-asg`, one task per host,
`distinctInstance`) is bounded by `vl-ledger-db` connections: `DB_POOL_SIZE` per task (25, task definition) against
`max_connections` (400, parameter group `vl-ledger-pg15`), i.e. 16 tasks. Adding tasks beyond the connection budget does not
add capacity: tasks 17-18 (the ASG allows 18 hosts) exhaust connections and crash-loop.

- Do **not** fail over or reboot `vl-ledger-db` for load.
- Prefer shedding asynchronous callers (Peak Freeze §4.1(f), documented here):
  - webhooks-dispatcher: `/vl/prod/webhooks-dispatcher/max_concurrency` (per task, reloaded every 30 s) or
    `/vl/prod/webhooks-dispatcher/consumer_paused`;
  - enable ledger-writer admission control: `/vl/prod/ledger-writer/admission/enabled`, with per-caller quotas under
    `/vl/prod/ledger-writer/admission/quota/<caller>` (req/s);
  - checkout outbox replayer: `/vl/prod/checkout-api/outbox/replay_rate` (req/s, fleet-wide).
- Relax gradually once backlogs drain.
- Each change: `CHANGE:` record in the incident channel within 15 min (Incident Process §6).
