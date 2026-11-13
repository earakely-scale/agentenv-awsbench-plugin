# SRE-RB-14 acme-renewer

Nightly ECS scheduled task (EventBridge Scheduler `acme-renewer-nightly`, 02:00 ET) that renews DNS-01 TXT records in
`acme.vantleaf.com` for ~2,400 merchant custom domains in batches of 50. It shares the account-wide Route 53 change rate with
everything else in vl-prod (OPS-4398).

- Stopping tonight's run (`aws ecs stop-task`) is harmless: certificates renew at least 20 days before expiry and tomorrow's
  run picks up where this one left off. Post a `CHANGE:` record and tell the owner (#sre).
- Do not edit the schedule during an incident.
