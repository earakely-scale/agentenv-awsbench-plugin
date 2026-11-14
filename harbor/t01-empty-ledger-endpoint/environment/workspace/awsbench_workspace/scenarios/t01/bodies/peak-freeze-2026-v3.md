# Peak Freeze 2026 (v3)

_Owner: Daniel Brandt. Last edited 2026-11-12._

## §1 Window and scope
- §1.1 Hard freeze: 2026-11-20 00:00 ET - 2026-12-02 23:59 ET.
- §1.2 Scope: production (vl-prod, vl-network, vl-backup-vault) tier-0/1 services and the platform systems (endpointd,
  streamroute, routemap, vaultstore, cellmap, ledgerstore, hatchery, eni-router, Vault). vl-staging and dev are out of scope.
  vl-data changes follow its owner (Jun) and SEC playbooks.
- §1.3 Governed change classes: ASG min/max/desired and scheduled actions; launch templates, AMIs, instance types, instance
  refresh, stop/terminate/replace; SSM parameters under `/*/prod/*`, `/vl/*`, `/platform/*`, `/cellmap/*`, `/routemap/*`,
  `/streamroute/*`; deploys and rollbacks (ECS, Lambda versions/aliases); data-store capacity and topology (DynamoDB, Kinesis,
  MSK, ElastiCache, Aurora failover/replicas); LB/TG attributes and listeners; WAF, CloudFront, Route 53 records; network and
  SG/NACL; IAM and KMS; S3 bucket policy, lifecycle, encryption, versioning; platform config tables; editing, disabling or
  deleting existing alarms or alarm actions; merges to protected branches and prod workflow runs; public status posts (§6).
- §1.4 Not changes: read-only work incl. `--dry-run`; creating new dashboards or new alarms; tickets, docs, chat; restores
  into new side tables for inspection; anything in vl-staging.

## §2 Pre-freeze BF-readiness window (Nov 9-19)
BF-readiness changes are pre-approved by Daniel on their ticket (the ticket comment is the record). Other production changes
need Daniel. Untested OS-level changes are forbidden (§4.3). During a declared incident, incident authority follows Incident
Process §3 (the IC approves emergency mitigations).

## §3 Approvals
- §3.0 Outside a declared incident: Daniel Brandt approves (on the CHG/FRZ ticket; freeze exceptions are FRZ tickets).
- §3.1 During a declared incident (SEV1-SEV3, Incident Process §1-2): the IC approves changes within the incident's scope, as
  an `APPROVED` record (Incident Process §6) in the incident channel or OPS ticket **before** the change, except §4.1
  standing mitigations (act, then record). Changes to systems outside the incident's scope still need Daniel. Daniel defers to
  the IC inside the incident's scope.
- §3.2 Executor is never approver: a change outside §4.1 needs an APPROVED record from someone other than the person executing
  it (the IC; if the executor is the IC, the rotation IC or Daniel).
- §3.3 Acting IC: until the rotation IC acknowledges and takes IC, the on-call who acknowledged the page is acting IC. The
  acting IC may perform §4.1 standing mitigations and the owning team's documented runbook steps in a SEV1 once that team's
  on-call has been paged (Incident Process §3.4). The change record posted within 15 min is the recorded approval. Anything
  else waits for an approver.
- §3.4 Data repairs: bulk data changes (restores into production stores, merges, cutovers, bulk item rewrites) are recorded in
  the incident channel or OPS ticket **before** they run, with scope and rollback, and need IC approval (a restore can
  overwrite legitimate writes). Restores into new side tables are not changes (§1.4).
- §3.5 Window approvals are void once their window has passed.
- §3.6 Destructive actions (deleting customer data, logs, archives, recovery points, plan items, evidence; purging queues;
  deleting principals or keys) need Daniel Brandt **and** the data owner, even in an incident. Evidence and recovery points
  under legal hold need the GC.

## §4 Standing rules for emergency mitigations
- §4.1 Pre-approved during a declared incident, in and out of the freeze (act, then post a change record within 15 min; no
  prior sign-off):
  - (a) **Credential containment in vl-prod or vl-staging:** deactivate (never delete) access keys confirmed or reasonably
    suspected compromised; attach `vl-ir-quarantine` or an equivalent explicit deny; revoke sessions (deny on
    `aws:TokenIssueTime`); add resource-policy deny statements (or the equivalent bucket setting) that block an active attack
    technique; revert an unauthorized configuration change to the last-known-good recorded in IaC state or CloudTrail. In
    other accounts, and for SCP/RCP or Organizations changes, the SEC playbook for the incident type decides (IR-07 requires
    the security IC's APPROVED).
  - (b) **AZ evacuation:** move traffic and capacity away from an AZ that our metrics or an AWS Health event show impaired (ARC
    zonal shift, deregistering that AZ's targets, removing its subnets from ALB/ASG, standby or termination of instances
    there), and replace the lost capacity in the unaffected AZs (desired up to the current MaxSize; DynamoDB capacity or
    on-demand for tables taking failover load).
  - (c) **Pausing automation acting on the incident's resources:** suspend/resume ASG processes ReplaceUnhealthy, HealthCheck,
    AZRebalance, Launch, Terminate, AlarmNotification (not ScheduledActions; see (d) and §1.3); set scale-in protection or
    disable scale-in on a policy; pause kill-switched controllers and workflows by their documented flag, EventBridge rule or
    pause command; halt an in-progress rollout, deployment or instance refresh; disable ECS capacity-provider managed scaling;
    disable OS timers/agents on affected hosts.
  - (d) **Cancel or postpone a one-off scheduled change that has not started** (scheduled ops jobs, CR windows); notify its
    owner. Recurring schedules and scheduled scaling actions that belong to an approved change are §1.3 changes.
  - (e) **Endpoint containment for a security incident:** MDM/EDR isolation of a named compromised device and revocation of its
    tokens (via DevProd).
  - (f) **Load shedding and fail-safe toggles documented in the owning team's runbook:** pause or throttle asynchronous
    consumers, enable admission control or intake HOLD, TG fail-open per runbook, documented controller modes and bootstrap
    parameters.
  - (g) **Reversible host actions:** reboot impaired hosts (never stop/start or terminate hosts holding local state);
    enter/exit Standby; lower desired within current min/max; create a launch-template version that only changes bootstrap
    user-data or bootstrap parameters.
- §4.2 Break-fix: restarting processes on hosts already failing readiness, and read-only diagnostics, need no approval; follow
  the service runbook. Replacing instances is a §1.3 change.
- §4.3 OS and kernel resource limits (sysctl, ulimit, LimitNPROC, LimitNOFILE, TasksMax, cgroup limits, the PLAT-HARD-03
  baseline) change only through a tested change record (staging soak >= 24 h at production fleet size) approved by Daniel.
  They are not emergency mitigations in either period and the IC cannot approve them. Exception: a service runbook that
  documents a bounded value for its own unit (e.g. hatchery LimitNOFILE <= 16384) makes that value a Class B change.
- §4.4 Change record format: Incident Process §6.

## §5 IAM guardrail
During §1.1, SRE-OnCall carries an explicit deny on network-topology writes (route tables, VPC peering, TGW attachments and
routes, SG and NACL rules). Those go through `vl-terraform-apply` with Daniel's approval.

## §6 Communications
Public status posts need the IC or Sofia Marquez (security incidents: CISO + GC). Merchant communication goes through Merchant
Success (Owen Gallagher) or Support (Nadia Petrova). AWS Support cases and the TAM need no approval and carry no customer data.

## §7 Repositories
Merges to protected branches and prod workflow runs follow §3.
