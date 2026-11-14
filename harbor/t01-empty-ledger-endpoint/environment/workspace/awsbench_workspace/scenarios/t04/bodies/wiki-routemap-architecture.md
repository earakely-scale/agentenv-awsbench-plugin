# Routemap architecture (2026)

Routemap is the merchant payment-routing control plane. Owner: Ravi Menon. Platform on-call: the platform schedule.

## State
- DynamoDB global table `routemap-state` (us-east-1, replica us-west-2) is the source of truth. One `ROUTE` item and one `LIMITS` item per merchant (PK `merchant_id`, SK `rec`).
- A ROUTE item holds identity (`route_id`, `idem_ns`), `lifecycle`, `route_version`, `publish_mode` (AUTO or SUPPRESS), `route_spec` (cells, weights, pool, fallback cell, pinned, residency) and `settlement_profile`.
- Idempotency keys in `vl-idempotency-keys` are namespaced by `idem_ns`. A new `idem_ns` means client retries are no longer deduplicated.
- SSM `/routemap/prod/state_table` names the table every component uses. Switching it is a cutover.

## Data plane
- `routemap-publisher` compiles ROUTE items into route configs and pushes them to the payments-gateway route cache.
- The data plane keeps the last accepted config per merchant and accepts a push only when `route_version` is higher than the loaded one. Equal versions are ignored; lower versions are rejected with `STALE_VERSION`.
- Writes with `publish_mode=SUPPRESS` are not pushed. `rmctl publish` pushes the current item regardless of publish_mode and sets it back to AUTO.
- A merchant whose loaded config has no cells fails every authorization with `ROUTE_NO_HEALTHY_CELL` (fallback `cell-drain` accepts nothing).

## Workflows (ECS RunTask from EventBridge)
| Workflow | Schedule | Writes | Kill switches |
|---|---|---|---|
| scaler | every 10 min (:00, :10, ...) | read-modify-write of the full item; adjusts weights for merchants whose traffic shifted > 20% | rule `routemap-scaler-tick`, SSM `/routemap/prod/workflows/scaler/enabled`, scope `/routemap/prod/workflows/scaler/scope` (`all` or `canary:<pct>`) |
| rebalancer | hourly at :35 | moves unpinned merchants off hot cells | rule `routemap-rebalancer-tick`, SSM `.../rebalancer/enabled`, `.../rebalancer/scope` |
| reconciler | :05 and :35 | none (reconcile-only; logs a drift summary) | rule `routemap-reconciler-tick`, SSM `.../reconciler/enabled` |

The workflow images vendor their own copy of the routemap model.

## API
`routemap-api` (ECS service on vl-prod-core) serves DescribeRoute, ListRoutes, CreateRoute, UpdateRoute for merchant-dashboard and merchant-onboarding. Feature flags: `/routemap/prod/api/update_route_enabled`, `/routemap/prod/api/create_route_enabled`.

## Tooling
`rmctl` on rm-admin-01 (instance profile vl-rm-admin-role). Every command is written to `/routemap/prod/audit`. Recovery: see `docs/state-recovery.md` in vantleaf/routemap.
