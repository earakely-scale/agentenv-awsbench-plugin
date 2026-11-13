# endpointd design (PLAT)

_Last edited 2026-09-30 by Kwame Asante._

**What it does.** endpointd keeps weighted A records for the 41 managed endpoints of the private zone `internal.vantleaf.io`
in step with the fleets behind them.

**Planner** (`endpointd-planner-1`). Every 60 s builds one plan for all managed endpoints from fleet observations: an instance
that is running and whose health endpoint is ready gets weight 100; running but not ready gets 0; terminated is removed.
It writes `plan#<gen>` (status PENDING) to DynamoDB `endpointd-plans` and `prod/plans/gen=<gen>.json` to
`s3://vl-endpointd-plan-archive`. Record set ids are `g<gen>-<azid>-<n>`, TTL 60.

**Enactors** a, b, c (one per AZ: `endpointd-enactor-a` use1-az6, `-b` use1-az1, `-c` use1-az4; independent). Each takes the
newest plan, checks once that its generation is newer than the highest applied generation, then applies endpoint by endpoint,
writing `applied#<fqdn>` (APPLYING -> APPLIED) around each Route 53 change. After a full pass it verifies the live records
against the plan and sets the plan's status APPLIED with `verified_at`.

**Cleanup.** After applying generation G an enactor deletes plan items with generation < G-20 and every Route 53 record whose
set id carries a deleted generation.

**Last-known-good plan.** The newest plan whose apply completed with `verify=ok` (status APPLIED with `verified_at`).

**Invariant.** Every `applied#` pointer references an existing plan whose entry equals the live records (values, weights,
set ids, TTL). An enactor that finds the invariant broken halts its apply loop until a human repairs it. An enabled enactor
overwrites hand-made records with the newest plan.

**Kill switches.** `/platform/endpointd/prod/{planner,enactor-a,enactor-b,enactor-c}/enabled`; components poll every 10 s.

**epctl** reference: see `epctl --help` on any endpointd host.

**Known issue:** PLAT-412.
