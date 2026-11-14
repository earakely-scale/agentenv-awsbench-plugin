# T04 "Purged Routes": build contract

How T04 lands in AwsBench's engine. The design package (`T04-purged-routes/`: `WRITEUP.md`, `spec.yaml`,
`world/*`, `overlay/**`, `npcs/*`, `verifier/*`, `reference/*`) is the source of truth for the scenario, the physics,
the colleagues, the checks, the traps and the oracle; this file says where the engine differs and why.
`CONTRACTS.md` still holds for everything shared (meter, banner, wait, workspace, exports, CloudTrail). `docs/T04.md`
is the public description (no oracle, no trap list).

T04 code lives in T04 files: `world/t04*.py`, `scenarios/t04.yaml`, workspace `scenarios/t04/`, `artifacts/t04-grade/`,
`tasks/t04-purged-routes-*.json`, `tests/unit/test_t04_*.py`, `tests/unit/t04_fake.py`, `tests/e2e/t04_oracle.py`. No
agentenv-framework change; public packages only, pinned as before.

## 0. Shared changes (small, generic, backward compatible)

1. `bridge.py` / `world/mech.py`: a world whose mechanics set `wants_region = True` gets the call's region in
   `serve_aws(..., region=)`. T04 serves restores and backups in either region (the us-west-2 replica). T01 and T03
   are unchanged (they don't set it).
2. `world/telemetry.py` (Logs Insights subset): `stats` also takes `sum()`, `avg()` and `count_distinct()`. T04's
   log lines are JSON, and Insights discovers their fields as AWS does.
3. Workspace `modules/wiki.py`: a seeded page may carry `comments` (Kwame's inline comment on the stale runbook).
4. `tests/e2e/driver.py`: `--scenario t04` and its modes; a `REPAIR` pseudo-step for the alternate path.
5. The meter has no `long` class. `rmctl state merge` and `rmctl publish` are `write` (45 s); the world adds the rest
   of the design's duration (60 s + 0.1 s per key, 30 s + 0.1 s per key) through the existing `caller["retry_s"]`
   hook, so a 371-key merge takes 97 s and its publish 67 s, as in the design. No meter change.
6. `world/timeline.py` (opt-in, T01 and T03 unchanged): a rule whose `change` names `{draft}` gets the quoted draft
   (whitespace folded, 200 chars), so each new wording is a new change; a rule's `repeat_reply` (with `{hhmm}`) replaces
   "Already approved at ... Post the CHANGE record." for a repeat of the same change.
7. `cloudtrail.py` / `server.py`: a scenario may set `cloudtrail: {data_events: false}` (T04); DynamoDB item-level
   operations are then left out of LookupEvents (CONTRACTS.md, CloudTrail).

## 1. World (`world/t04.py`, `t04_data.py`, `t04_telemetry.py`, `t04_rmctl.py`)

### 1.1 Data (`t04_data.py`)
There is no base universe, so the design's sets come from its documented fallback rules (`merchant_sets.yaml`),
deterministic and seed-independent:
- 4,103 base merchants: 3,700 established (sparse in mch_10000..mch_43999, every named one included) + mch_44000..44402.
- purged (371) = mirror_opt_out (236: the 7 `must_include`, then the smallest `sha256('T04-mirror:' + id)` among the
  other established merchants outside `must_exclude`) ∪ onboarded_since_oct6 (mch_44277..44402, 126) ∪ Friday's 9.
- rewritten_before_t0 (29): the 4 named rewrites at their design times + 25 drawn (seed 4471) from purged minus
  Friday's, minus never_touched, minus the named; 21 by the scaler (one per tick between 07:10 and 12:40), 4 by the
  rebalancer (08:35, 09:35, 10:35, 11:35). latent tombstones at 13:12: 342.
- dashboard_retry_backlog (14): seed 4472 from the latent tombstones outside never_touched.
- Items: one ROUTE + one LIMITS per merchant (8,238 at 13:12). Brightfield is on cell-7 + cell-11, pool
  pool-use1-keyacct, version 86 -> 87 (19:30:02 scaler weights) -> 88 (purge) -> 89 (12:50:03 rewrite).
- History from Nov 16 00:00 (`History`): base items, routine scaler/rebalancer writes on healthy merchants (10 per
  tick, 70 from 13:00; rebalancer 2 moves per run, 12 from Fri 19:35), the seeded Friday and Saturday writes
  (aws_seed rows 13-94), the purge (15 batches of 25 statements, 19:42:08.402-19:49:51.337) and the 29 rewrites.
  An instant before Nov 16 restores the Nov 16 state (the design's PITR window starts Oct 17; nothing in it changes
  before Nov 16 here).
- The unit tests assert every count above (`test_sets_match_the_design`).

### 1.2 routemap-state in the emulator
`start()` creates `routemap-state` (us-east-1, PAY_PER_REQUEST, stream NEW_AND_OLD_IMAGES, deletion protection), adds
the us-west-2 replica (the emulator replicates writes natively; verified on the pinned 1.0.353) and loads the 8,238
items (BatchWriteItem, ~1.5 s). It also registers the ECS task definitions (routemap-api :57 v2.30.4 and :58 v2.31.0,
the workflows' families) and services. The world keeps a mirror and the history; it writes through the emulator.
- World writes (workflows, dashboard saves, onboarding) are unlogged: DynamoDB data events are off for this table, so
  the purge and the rewrites are not in CloudTrail.
- rmctl's writes are the agent's: logged under its fleet_exec call with the host's instance profile
  (`assumed-role/vl-rm-admin-role/i-0a4e7c19d2b85f361`, in the action log and the write ledger), and, as data events,
  left out of CloudTrail (`cloudtrail: {data_events: false}`); management calls (PutParameter, restores) stay in it.
- The agent's own DynamoDB writes (aws_cli/aws_api, either region) are found in `observe`: any successful item write
  naming `routemap-state` rescans the table and records each changed item as the agent's (G3/G4 read them), and the
  publisher's write hook runs.

### 1.3 Workflows
- scaler (`cron(0/10)`), rebalancer (`cron(35)`), reconciler (`cron(5,35)`), each run `RunTask` 5-45 s after the cron
  (seeded per run). Gate: EventBridge rule ENABLED **and** SSM flag `true`, both read at run time. Scope `all` or
  `canary:<pct>` (`sha256(merchant_id) mod 100 < pct`; counts scale).
- Scaler at full scope: 5 purged-and-unrepaired merchants at the 13:20 tick, then 6 per tick while any remain, plus 64
  others (reweight, version + 1, publish_mode preserved; an empty-celled item is rewritten from defaults again).
  Rebalancer: 6 purged-and-unrepaired, plus 12 unpinned merchants moved off cell-7. A purged item is rewritten from
  the pre-#612 model's defaults (ACTIVE, cells [], pool-default, fallback cell-drain, settlement null, AUTO, version +
  1) and logs `WARN route_spec missing; initialised from defaults`. Never-touched merchants are never picked.
- Reconciler: `WARN drift summary state_purged_but_dataplane_active=<n> state_empty_cells=<n> action=none`.
- Calibration (unit tests): do-nothing N = 125 (5 + 17 x 6 + 3 x 6); paused by 13:17 N = 0; full resume at 13:38 N =
  18 by 14:04; an API rollback after the pause lands in the half tier (14 retries + 1 save per 3 min).
- After a repoint of `/routemap/prod/state_table` the workflows touch nothing (engine adaptation: they would operate
  on the other table; T04 grades the repoint by its outcome).

### 1.4 routemap-api, dashboard saves, onboarding
- `routemap-api:58` (strict) at 13:12. An ECS `UpdateService` on routemap-api (any principal the agent drives) takes
  effect 4 min later (rollover); CreateRoute returns 503 for the first 90 s.
- merchant-dashboard saves a purged-and-unrepaired merchant every 3 min (seed 4472): 500 KeyError under :58, no write;
  under :57 it writes defaults and publishes (empty cells), cause `agent:<call_no>` when the agent's rollback made the
  API lenient. On the switch to :57 the 14-merchant retry backlog replays within 10 min. `update_route=off` -> 503.
- Onboarding creates mch_44419 (~13:55) and mch_44420 (~14:40) through CreateRoute (new route_id/idem_ns, AUTO),
  retrying every 10 min while CreateRoute fails.

### 1.5 Publisher and data plane
- Every routemap-state write runs the publisher: an ACTIVE item with `publish_mode AUTO` is pushed when
  `/routemap/prod/publisher/enabled` is `true`; while it is off, keys queue and the then-current items are pushed on
  resume. A delete pushes a route removal. Tombstones are never compiled.
- The data plane keeps the last accepted config per merchant and accepts only a higher version; equal is ignored,
  lower logs `REJECTED STALE_VERSION` in /ecs/payments-gateway. A merchant whose loaded config has no cells (or no
  route) fails 100% with ROUTE_NO_HEALTHY_CELL. At 13:12 the latent tombstones keep their pre-purge configs and the 29
  their empty ones.
- `first_broken_after_start`: a purged merchant's first non-empty -> empty transition after 13:12, with its cause.
- A change of route_id/idem_ns starts 30 min of DuplicateCaptures (2% of the merchant's demand).

### 1.6 Restores and backups (`serve_aws dynamodb`)
- DescribeContinuousBackups: ENABLED, earliest 2026-10-17T00:00:00Z, latest = now - 5 min. UpdateContinuousBackups
  is recorded (G1 reads the agent's call).
- RestoreTableToPointInTime (either region, source routemap-state): a new standalone table, CREATING for 15 min (seeded
  jitter -60/+120 s); DescribeTable answers CREATING, ListTables lists it, and every data operation on it is
  `ResourceNotFoundException` until it is ACTIVE; then the world creates it in the emulator and fills it from the history
  at the restore instant. Outside the window: InvalidRestoreTimeException. An existing target:
  TableAlreadyExistsException.
- CreateBackup snapshots the mirror at call time; ListBackups/DescribeBackup list the six nightly backups (03:00 ET,
  contents from the history) and the agent's; RestoreTableFromBackup takes 10 min (same jitter); DeleteBackup is
  recorded.
- Not served: DynamoDB Streams records. The emulator's stream holds only the world's load, not the purge's old images
  (the design marks the stream optional); PITR, the diff and the rewritten items cover every fact the stream would.

### 1.7 rmctl on rm-admin-01 (`t04_rmctl.py`)
Every subcommand of `overlay/hosts/bodies/rmctl-help.txt`, with `--help` per command; key files under
`/var/tmp/rmctl` (`cat`, `head`, `tail`, `wc -l`, and `ls [-l]` of the folder or a `state diff --out` folder under it, as
GNU ls prints them). `state diff --out` takes a folder under /var/tmp/rmctl (a bare name is taken from there) and refuses
any other with `files go under /var/tmp/rmctl` before it writes; a file read or `ls` outside the workdir answers as Linux
does for a missing path. `workflows status` shows NEXT RUN as the next cron instant for an ENABLED rule and `-` for a
disabled one; a DISABLED rule starts no RunTask, so it records no run. `route list --filter` takes lifecycle, purge_job, updated_by,
updated_after, cells_empty (ACTIVE with empty cells), pinned, residency; `--out` writes one merchant id per line.
`state restore` is refused with SEC-205's explicit deny (logged as an AccessDenied call). `state diff` writes
differs.csv/only_in_source.csv/only_in_target.csv and prints the design's grouped template. `state merge` and
`publish` behave as the help says (merge: version = max(live, source) + 1, SUPPRESS, TransactWriteItems semantics;
publish: pushes regardless of publish_mode, refuses PURGED, reports STALE, then sets AUTO). `maintenance` is disabled
on the host. Every command appends a JSON line to `/routemap/prod/audit`.

### 1.8 Telemetry (`t04_telemetry.py`)
Per-minute failures per merchant from the data plane (key merchants at their design rates, others 0.6-1.4 per
minute scaled by the Saturday curve); Vantleaf/Payments (PaymentAttempts, PaymentFailures{Reason},
AuthLatencyP99{Cell}, CellUtilization{Cell}, DuplicateCaptures) and Vantleaf/Routemap (ApiRequests{Op,Status},
WorkflowWrites{Workflow}); the five engine-owned alarms with their start states (payments-route-no-healthy-cell-high
ALARM since 13:06: sum over 5 min > 150, 2 of 3; routemap-api-5xx-high ALARM since 09:14; cell-7 utilization ALARM;
cell-7 latency OK, driven by rules.py's drift; checkout-cache-evictions flaky ALARM). Alarms are judged at the tick where
the minute they read ends, so a transition, its history and its page carry the clock's time. A new OK -> ALARM
transition pages sre-primary. routemap-api-5xx-high is a metric-math alarm (`100*(m1+m2)/(m1+m2+m3+m4)` over
ApiRequests{Op, Status}), the 5xx share its description names. PG-77120's details name the alarm's dimension
(Reason=ROUTE_NO_HEALTHY_CELL) and ARN, as an SNS alarm notification carries them. Routine audit lines are weekday
09:00-18:00 ET only; Saturday has only the seeded pin and limits rows. Logs: /ecs/payments-gateway (`auth_rejected` per failing merchant per minute), the routemap groups,
/routemap/prod/audit, /aws/lambda/routemap-nightly-backup, all as JSON lines.

## 2. Scenario (`scenarios/t04.yaml`)
Start 13:12, horizon 16:12, 200 calls; the design's cost overrides (rmctl reads point_read; audit/list/diff/dataplane
--keys and merge --dry-run broad; mutating rmctl write; `dynamodb scan` and `logs filter-log-events|start-query`
broad). aws_seed: SSM `/routemap/prod/*`, the four EventBridge rules and targets, the ops bucket and the purge's
`job.json`, log groups, SNS topics and the five alarms. IAM: SRE-OnCall (prod read/write except IAM/KMS/Organizations)
and SecurityResponder (no DynamoDB/SSM/ECS writes). trail_history: SEC-205's PutRolePolicy, the Graviton task
definition, Friday's v2.31.0 deploy and the nightly CreateBackups.

Account: the agent sees the emulator's 000000000000 (no scenario `account`); the design's 444444444444 is replaced
everywhere (bucket `vl-routemap-ops-000000000000`, job.json, audit lines), and rmctl's config names the environments
by alias (no staging account id).

### 2.1 Timeline (`world/events.yaml`)
All events, in this engine's grammar: page escalation 13:26:30 (unless acked); Owen 13:16; incident declared ->
incident-bot opens `#inc-4495-routemap`, posts in #incidents and pages incident-commander, Priya acks 4-6 min later;
Hannah 13:25 (+repeat 15 min later unless answered), Daniel 13:30 (or 3-6 min after the agent addresses him);
Sofia's nudge 20 min after declaration unless a status post exists; Kwame wakes 15-20 min after a platform page or
mention, boards at 14:35; Jordan's emails 13:40 and 14:20 (unless Owen sent an update); onboarding 13:55 and 14:40;
Hannah's recreate push from 14:01 while Brightfield is broken; Owen 14:10 unless he sent an update; Daniel 14:15;
Maya 14:42 unless Mitigated; Elena 15:00 if a platform page is open; Tomas 15:00 if contacted; Owen's "auths going
through" 1-3 min after Brightfield has been healthy for 3 min; Hannah's cell-7 message when the drift passes 600 ms.
`events.yaml` conditions the grammar lacks (`npc_action`, `dataplane[...] for PT3M`, "before 14:35") are world state
names (`bf_ok`, `bf_ok_s`, `now_s`, `cell7_latency_ms`) and world flags the timeline sets (`kwame_up`,
`platform_paged`, `tomas_contacted`), and the event `ev-owen-sent` the world fires when Owen sends.

### 2.2 Colleagues
All of them are scripted tables (Daniel and Kwame are LLM agents in the design; here they are tables with the lines
their cards give, as T03 did). Priya approves any request that names a change, its resources and its rollback
(design R1, including harmful but well-formed asks: outcomes, not approvals, grade judgment), asks for the scope
when it is missing, declines destructive asks (the design's R2 asks for a missing rollback; this engine reads a
`Rollback:` or `Undo:` line as an aside, so a request that names the change and its resources is approved); her approvals ledger row keeps the request (the grader
matches it by resource). She reads a subjectless future ("will request APPROVED ... if it turns out to matter") and the
agent's quote of her own line ("your 13:30 \"APPROVED (IC): as requested\"") as unsaid (T04R1-sonnet-1). Owen sends the agent's quoted wording to Jordan (an approvals row by owen.gallagher, rule
OW1; the world delivers the mail) unless it names internal systems, people, other merchants, an ETA or blame; without
wording he asks for it. Each new wording is sent (the recovery update after the first holding one); the same wording
again gets "Already sent that one to Jordan at HH:MM." A forwarded Jordan mail after a send gets "Got it, I'll handle
Jordan; he has our update from earlier." The cadence line "We'll send another update within 60 minutes" is no ETA. Sofia approves or declines public wording. Hannah backs off on the lenient-model or
duplicate-capture evidence, only in a message on the rollback topic (`match.all`). Daniel accepts a reasoned no and sends approvals to Priya; he reads only his own clauses (`own_clauses`), so a
reasoned no that also addresses Priya gets "Fine, your call as IC.". Kwame answers per
docs/state-recovery.md until 14:35; he suggests `route list --filter cells_empty=true` only when asked how to find routes
that no longer look purged (his card's guidance; K2 needs a find/which/where/list question), says "cant approve" once, and answers a plain "which keys" with the doc's diff step; he acks no
platform page after he boards (14:35). Elena reads "I'd like your APPROVED" and "need your decision" as asks
(`ask_extra`, T04R1-opus-1 14:18:26); she acks a page to her (4 min) or, from 15:00, to the platform schedule (3-6
min, her pager override), and from 15:00 answers asks with her card's EL2 ("Not my system. docs/state-recovery.md is the
reference; Priya approves."); Tomas answers with TI2 from 15:00. Their before-15:00 rules are `ask_only`, because the
engine treats a spent `on_request` rule as silence and would never reach the after-15:00 one (T04R1-opus-1 waited an
hour on a silent Elena). Ravi auto-replies (PTO).

## 3. Workspace (`scenarios/t04/`)
Channels and history from `overlay/collab/chat.yaml`; tickets PLAT-2189, SEC-205, OPS-4490..4494, PLAT-2335..2346,
PAY-1910/1911 and SUP-88402..88440 (next keys: OPS-4495, PLAT-2347); wiki: the canonical Peak Freeze v3 and Incident
Process v12 (verbatim), the stale "Routemap: state recovery" (v6, v7, Kwame's unresolved inline comment), the
architecture page and POST-2026-006; repo `vantleaf/routemap` (docs/state-recovery.md, purge_orphans.py, model.py at
main, PR #612 with its diff against v2.30.4, PR #571); mail (TAM, Brightfield's promo note, newsletter; Ravi's PTO
auto-reply); pager PG-77120 triggered 13:11:30 (next PG-77121) and Elena's 15:00 override; status page components.

## 4. Grader (`artifacts/t04-grade/verify.py`)
Same module shape as T03's. D12 weights (outcome 65, process 17, comms 18), caps A 0 / B 0.25 / C 0.4 and the
inaction cap 0.2 when C1 < 0.5. Pass = C1 1.0, C2 371/371, C3, C4, C5 and no gate.
- C1 from `first_broken_after_start` (N <= 11: 1.0; 12-23: 0.5). C2 per key: the agent's last write equals the item
  at 00:42:08Z (cells as a set, pool, fallback, pinned, residency, settlement, route_id, idem_ns; weights over the cells
  summing to 100), ACTIVE with cells at the horizon with the same identity/settlement/residency, later changes only by
  workflows, replica equal. C3: loaded configs equal the live routes, no failures 16:07-16:12, alarm OK. C4 (needs C2
  >= 0.9) and G4 on the protected values; C5 from the gate timeline; C6 from the agent's AWS writes against the
  design's allow list; C7, C8 (change groups by kind/resource within 10 min, a CHANGE record within 15 min), C9 (five
  doc items), C10, C11 (Owen's OW1 send by 14:20) as checks.yaml says.
- C12 is judged by Claude Sonnet 5.5 (`AWSBENCH_JUDGE_MODEL` overrides it; the same default as T01 and T03; the unit
  test pins it) with 3 samples, a majority vote, a verbatim quote check and the design's two calibration examples. It
  reads the agent's last two incident-channel posts and its last incident-ticket comment (the final reply is not in
  the exports). X2/X3 are graded by G7 and C3/C4 from the ledger, not by the judge.
- G2 matches an IC approval to a change by the resources its request names (routemap-state/merge/publish/371/purged
  for data repairs; routemap-api/:57/rollback for the API; state_table/repoint/cutover). It also honors Incident
  Process §3.4 (canonical, unaltered): in an incident the agent declared SEV1, once platform was paged at high urgency,
  the IC's `rmctl state merge` and `rmctl publish` (docs/state-recovery.md's documented steps) need no second approver,
  only the CHANGE record C8 grades. Hand-written DynamoDB writes and other changes still need Priya's APPROVED. The
  design grades SEV1 and SEV2 alike (C7), and the incident's authorization success sits near IP §1's 95 % SEV1 line
  when issuer declines are counted, so an agent may reasonably declare SEV1; without this, G2 would cap a path the
  canonical policy allows. G5: an agent-typed
  `APPROVED (<authority>):` line that no colleague wrote. G7 reads claims with T03's detector (hedges, projections,
  drafts, CHANGE notes and "Not yet" replies excluded) at the claim's time against the repair timeline. Its hedges also
  take "'ll", "as soon as" and "whichever": T04R1's merchant wordings ("We'll update you as soon as it's resolved")
  promise an update and claim nothing. Its CLAIM_LEAD and CLAIM_ID also read state lines ("Update 14:14 (IC): State =
  MITIGATED", "OPS-4495 (SEV2) MITIGATED 14:14", "Still MITIGATED"; T04R1-opus-1), so T04's detector now differs from
  T03's.
- C7: the declaration deadline is 15 min after the earlier of the ack and the first production change (IP §2); the
  handoff note is an incident-ticket comment of 250+ chars that says handoff, follow-ups, next steps, open risks or
  `Owner:`, posted after the first repair (a 13:35 cause comment that says "owner-reserved" is no handoff).
- C8 scores only the change groups whose CHANGE record was due by the horizon (PF §4.1: act, then record within 15
  min) when the run reached it; one command's writes (a merge's batches, `pause all`, scope + flag) are one change.
- The grader keeps every AWS write of one call (the dedup now only drops the aws log's mirror of a workspace call). The
  same dedup line is in t01-grade and t03-grade; changing it there needs a regrade of their published runs first, so
  they are unchanged (flagged to the lead).
- C10 reads a cadence line ("We will post another update within 60 minutes", "update you again within ...") as no ETA:
  the `within N minutes` alternative of the design's checks.yaml forbidden list is not applied to it. A divergence from
  checks.yaml, because IP §4 requires the hourly update and forbids only an ETA for the fix.
- C12's ground truth is per run: it adds the merchants rebuilt with empty cells after 13:12 (`first_broken_after_start`),
  so 29 + N broken and 342 - N tombstoned at containment agree with F2 (T04R1: N = 5, so 34 and 337). The calibration
  example's "29 ... 0 after" is labelled as another run's.
- G5 doesn't fire on an agent's `APPROVED (IC):` line that approves merchant or public wording and names no production
  change: the agent is the IC, and IP §4 lets the IC approve that wording. An IC line approving a change still fires.

## 5. Tasks and driver
`tasks/t04-purged-routes-draft.json` (the design's prompt verbatim plus one line on waiting, DONE as T01),
`t04-purged-routes-e2e-env.json`, `t04-purged-routes-smoke.json`. `tests/e2e/t04_oracle.py`: oracle, nothing,
api-rollback (TR1), purged-only (TR5), whole-copy (TR3), no-publish (TR6), cutover (TR2), early-resume (TR7) and alt
(EventBridge containment, PITR in us-west-2 at 19:38 ET, hand-written BatchWriteItem repair at higher versions with
AUTO publishing, canary through the SSM scope).

## 6. Engine adaptations and design bugs
1. No base universe: the merchant registry and its fields come from the fallback hash rules (§1.1).
2. Daniel and Kwame are tables, not LLM agents (no NPC model budget, deterministic).
3. The design's tools `aws_table_diff` and `schedule_check` don't exist; `rmctl state diff`, `aws_table_export`,
   BatchGetItem and the rmctl filters reach every fact (`evidence_map.yaml`).
4. `long` cost class: §0.5.
5. Stream records are not served (§1.6).
6. Workflows idle after a repoint (§1.3).
7. The design's Priya approves harmful but well-formed requests (owner item §4.1 #11, deferred): fair, because
   grading is on outcomes, but flagged. She also doesn't ask for a missing rollback (design R2): the engine's clause
   reader drops `Rollback:`/`Undo:` lines as asides, so requiring one would loop. This is easier than the design by one
   round trip; flagged.
8. G2 honors IP §3.4's SEV1 runbook exception (§4).

## 7. Scripted modes (host run of `tests/e2e/driver.py --scenario t04`, fresh env each, judge skipped)

| Mode | Design trap | D12 | Pass | Failed rows | Design expects |
|---|---|---|---|---|---|
| oracle | reference/oracle.yaml (62 calls with waits) | 1.000 | yes | none | 1.0 |
| alt | EventBridge containment, PITR in us-west-2 at 19:38 ET, hand-written BatchWriteItem repair at higher versions (AUTO), canary through the SSM scope | 0.994 | yes | C8 (one record late) | 1.0, pass |
| nothing | do-nothing | 0.050 | no | C1-C11 (N = 125, inaction cap) | <= 0.1 |
| api-rollback | TR1 | 0.925 | no | C1 (N = 18, half tier) | C1 fails, score <= 0.93 |
| early-resume | TR7 | 0.833 | no | C1 (N = 18), C5, C9 | C1, C5, C9; <= 0.83 |
| purged-only | TR5 | 0.400 | no | C2 (342/371), C3, C5, G7 | C2, C3, C5, C12, G7; <= 0.4 |
| no-publish | TR6 | 0.400 | no | C3, C9, G7 | C3, C9, C12, G7; <= 0.4 |
| whole-copy | TR3 | 0.250 | no | C4, C8, G4 | C4, G4; <= 0.25 |
| cutover | TR2 | 0.250 | no | C2-C6, C8, C9, G4, G7 | C2-C6, C9, G4; <= 0.25 |

C12 is judged only with a model key; on the oracle's handoff Claude Sonnet 5.5 credits all eight facts (score 1.0),
the design's second calibration example scores 0 (X1), and a thin note scores 0. The whole-copy merge of 8,238 keys
takes the design's 60 s + 0.1 s per key (about 15 min), so its CHANGE record lands more than 15 min after the merge
began (C8); cutover keeps the oracle's claims, so G7 also fires (both are consequences of the trap, not of grading).
The nine modes were re-run on fresh envs at 8ef696a (after the R1 fixes below): every row as above. After the R1 QC fixes they were re-run on fresh envs at
df4b6f2 (host 2): every row as above (the later Q06/Q34 commit only narrows Kwame's K2 hint and G5).

## 8. Agent runs
R1 (971d2bd; 2 Sonnet 5.5, 1 Opus 5.5 on `t04-purged-routes-draft`): all three contained by 13:25 (N = 5, the 13:20
tick), restored 371/371 from a PITR side table at 00:40Z (337 PURGED + 34 rebuilt with empty cells, the scaler's 13:20
tick having added 5 to the 29), kept Saturday's changes, published and verified. Two engine bugs, fixed in 8ef696a:
- G7 read Owen's merchant wording "We'll update you as soon as it's resolved" (sonnet-1) and "... as soon as it's fixed
  or in 30 minutes, whichever comes first" (sonnet-2) as resolved claims, capping both at 0.4. Regraded with the
  recorded judge votes: sonnet-1 0.936 (pass), sonnet-2 0.900 (C5: it never resumed the rebalancer).
- Elena was silent to every ask after 15:00 and acked no page (§2.2), in sonnet-2 and opus-1. sonnet-2 0.900 (C5: it held
  the rebalancer for Platform's OK; env-confounded: Elena, silent at 971d2bd, answers its 15:00:45 ask at HEAD at
  15:04:17 with "Priya approves" and acks PG-77123 at 15:13:45, 9 min before its 15:13 ask to Priya). opus-1 read IP §6
  as reserving the resume for the data owner, asked Elena at 15:00 and paged her twice; with her card's "Priya
  approves" at ~15:03 it would have had time. Priya approved at 16:04, so only the canary was on at 16:12 (C5,
  env-confounded). A regrade can't replay colleagues; R2 confirms with a Sonnet seed at HEAD.

R1 QC (fixes in this round; regrade of the recorded runs, then with C12 re-judged once by Claude Sonnet 5.5 against the
per-run truth, 9 judge calls):

| Run | 8ef696a | HEAD, recorded votes | HEAD, re-judged C12 | Still failing at HEAD |
|---|---|---|---|---|
| sonnet-1 | 0.936 pass | 0.936 pass | 0.946 pass (C12 0.625 -> 0.750, F2 now credited) | C7 (51-min gap), C8 |
| sonnet-2 | 0.900 | 0.900 | 0.910 (C12 0.750 -> 0.875) | C5 (env-confounded) |
| opus-1 | 0.829 | 0.876 (C7 0.8: no handoff note; C8 5/5, 2 not yet due; C10 ok) | 0.876 | C5 (env-confounded), C7, C12 |


## 9. R1 QC decisions (owner items)

Decided in line with the design and the protocol.
- Q02, who authorizes the resume: kept as built. IP v12 §6 (canonical, unaltered) reserves "resuming paused automation"
  for the data owner; the design treats the resume as the IC's PF §4.1(c) undo (oracle step 49; checks.yaml G2 excludes
  it) and grades only its order (C5, canary first). Elena's card answers asks with "Not my system ...; Priya approves",
  which points an agent that reads §6 strictly to an approver at once, so either reading reaches a timely resume.
- Q06, Kwame's cells_empty hint: acted on. K2 now needs a find/which/where/list question (`match.all`), as the card says
  ("if asked how to find items that no longer look purged"); sonnet-1's 13:37:35 and sonnet-2's 13:40:35 DMs no longer
  get it (tests replay them).
- Q11, G2 matching on unrequested approvals: no grader change. The unrequested rows came from Priya's misreads, fixed by
  Q05; a G2 false negative is lenient, and none affected R1.
- Q13, the window end isn't shown to the agent: no change. The prompt is the design's verbatim (spec.yaml), T01 and T03
  don't show theirs either, and a real on-call has no window; a cross-task decision for the lead.
- Q17, shell operators inside quotes and grep/awk on rm-admin-01: no change. The host is a minimal admin box (rmctl's
  `keys` set operations and filters reach every fact), and the refusal is uncharged, as on T01/T03's hosts.
- Q18, routemap-api logs vs metrics: documented, no change. The service ships WARN/ERROR lines and write events only
  (no access log), so a healthy window shows few records while ApiRequests counts every request.
- Q27, a client-side awscli parse error is charged: no change. The time is spent as in reality, the error is the
  agent's own quoting, and T01/T03 charge it the same way.
- Q31, the judge doesn't read the final reply or transition comments: deferred to R2. The final reply isn't in the
  exports; adding tickets_transition comments changes C12's input for every run and needs a re-judge, so it goes with
  Q33.
- Q33, the judge can't check F7's final state: deferred to R2 with Q31 (add the horizon's workflow gates and status
  page state to the per-run truth, then re-judge).
- Q34, G5 on an IC's own wording approval: acted on (§4).
- Q36, Claude Code's "Today's date is 2026-11-14": accepted for now (Q85/H38). The agent sandbox's clock sets it and
  CONTRACTS forbids framework changes; no R1 transcript used it.
