# services/webhooks-dispatcher

Consumes `vl-webhook-delivery` (SQS) and delivers merchant webhooks. Each delivery reads the payment's ledger entries from
`ledger.internal.vantleaf.io`.

- Concurrency: `/vl/prod/webhooks-dispatcher/max_concurrency` per task (reloaded every 30 s).
- Pause: `/vl/prod/webhooks-dispatcher/consumer_paused=true` stops receiving; messages stay in the queue (retention 4 days).
- Autoscaling: Application Auto Scaling target tracking on `Vantleaf/Webhooks BacklogPerTask` = 300, min 6, max 40.
  **Lowering the desired count by hand is reverted by the autoscaler. Cap the scalable target's max capacity or suspend
  dynamic scaling instead.**
- On a ledger error the worker nacks with `ChangeMessageVisibility 0` (PAY-702: hot retry loop, no backoff).
- Redrive: maxReceiveCount 1000 (raised after POST-2026-014 so nack loops do not dead-letter good deliveries).
