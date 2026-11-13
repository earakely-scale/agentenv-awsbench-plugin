# Peak Freeze 2026 (v3)

_Owner: Daniel Brandt. Last edited 2026-11-12._

## §1 Window and scope
- §1.1 Hard freeze: 2026-11-20 00:00 ET to 2026-12-02 23:59 ET.
- §1.2 Scope: production accounts (vl-prod, vl-network, vl-backup-vault), tier-0/1 services and the platform systems
  (endpointd, streamroute, routemap, vaultstore, cellmap, ledgerstore, hatchery, eni-router, Vault). Staging and dev are out of scope.
- §1.3 What counts as a change: ASG sizes and scheduled actions; launch templates, AMIs, instance types, instance refresh,
  stopping or replacing instances; SSM parameters under `/vl/*`, `/platform/*` and other prod paths; deploys and rollbacks;
  data-store capacity, failover and topology; load balancer settings; WAF, CloudFront and **Route 53 records**; network,
  security groups, IAM and KMS; S3 bucket settings; platform config tables; editing or disabling alarms; merges to protected
  branches and prod workflow runs; public status posts (§6).
- §1.4 Not a change: read-only work (including `--dry-run`), new dashboards or alarms, tickets, docs, chat, restores into new
  side tables for inspection, anything in staging.

## §2 BF-readiness window (Nov 9-19)
BF-readiness changes are pre-approved by Daniel on their own ticket. Any other production change needs Daniel. During a declared
incident, incident authority follows Incident Process §3: the incident commander (IC) approves emergency mitigations.

## §3 Approvals
- §3.0 Outside an incident, Daniel Brandt approves (CHG/FRZ ticket).
- §3.1 During a declared incident (SEV1-SEV3) the IC approves changes inside the incident's scope, as an `APPROVED` record
  (Incident Process §6) **before** the change, except the §4.1 standing mitigations (act first, record within 15 min).
  Changes outside the incident's scope still need Daniel.
- §3.2 **Executor is never approver.** A change outside §4.1 needs an APPROVED record from someone other than the person
  making it. If the executor is the IC, the second approver is the rotation IC (Priya Raman during the freeze period) or Daniel.
- §3.3 Acting IC: until the rotation IC acknowledges, the on-call who acknowledged the page is the acting IC. The acting IC may
  run §4.1 mitigations and the owning team's documented runbook steps in a SEV1 once that team's on-call has been paged
  (Incident Process §3.4); the change record within 15 min is the record. Anything else waits for an approver.
- §3.4 Data repairs (restores into production stores, bulk rewrites, merges) are recorded before they run, with scope and
  rollback, and need IC approval.
- §3.5 Approvals for a time window lapse when the window ends.
- §3.6 **Destructive actions** (deleting customer data, logs, archives, recovery points, plan items or evidence; purging queues;
  deleting principals or keys) need Daniel Brandt **and** the data owner, even during an incident.

## §4 Standing emergency mitigations
- §4.1 Pre-approved during a declared incident (act, then post a `CHANGE:` record within 15 min):
  - (a) credential containment (deactivate, never delete, suspect keys; quarantine policies; revoke sessions);
  - (b) AZ evacuation away from an impaired AZ and replacing that capacity elsewhere;
  - (c) **pausing automation that acts on the incident's resources**: suspend ASG processes, pause kill-switched controllers by
    their documented flag or pause command, halt an in-progress rollout, deployment or instance refresh;
  - (d) cancelling a one-off scheduled change that has not started (tell its owner);
  - (e) isolating a compromised device (security incidents);
  - (f) **load shedding documented in the owning team's runbook**: pause or throttle asynchronous consumers, enable admission
    control, documented fail-safe modes;
  - (g) reversible host actions: reboot impaired hosts, standby, lower desired count within min/max, flush a node-local
    DNS cache entry. A flush needs a `CHANGE:` record like any other §4.1 action.
- §4.2 Restarting failing processes and read-only diagnostics need no approval.
- §4.3 OS and kernel limits change only through a tested change record approved by Daniel; never as an incident mitigation.
- §4.4 Record format: Incident Process §6.

## §5 IAM guardrail
During §1.1 the SRE-OnCall permission set cannot change network topology (routes, peering, SG/NACL rules).

## §6 Communications
**Class B changes during the freeze need a second approver**: the IC never approves their own change (§3.2); ask Priya Raman.
Public status posts need the IC (or Sofia Marquez). Merchants hear from us only through Merchant Success (Owen Gallagher) or
Support (Nadia Petrova). AWS Support and the TAM need no approval and get no customer data.
