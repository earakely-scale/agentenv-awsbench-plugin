# Incident Process 2026 (v12)

_Owner: Priya Raman. Last edited 2026-11-02._

## §1 Severity
- SEV1: checkout or authorization success < 95 % for 5 min; Hosted Checkout or Payments API unavailable; > 10 % of payment
  volume failing; ledger integrity or settlement/payouts at risk; a merchant-facing system down for all merchants (receipts,
  exports).
- SEV2: merchant-visible degradation of non-checkout paths (dashboards, webhooks, exports); payments failing for a key account
  or >= 10 merchants; checkout errors 1-5 %.
- SEV3: degraded with no merchant-visible failure. SEV4: cosmetic.

## §2 Acknowledge, declare, page
- Ack pages within 5 min; unacked pages escalate after 15 min. An acknowledged P1/P2 page is an active incident.
- Declare within 15 min of the ack or of the first production change, whichever is first: OPS issue of type Incident with
  severity, chat channel `#inc-<OPS number>[-<slug>]` (incident-bot creates it when it opens the issue), post in #incidents.
- SEV1/SEV2 auto-page the incident-commander schedule. Page the owning team's on-call at **high** urgency for any platform
  system involved.
- Security incidents: page security-primary; channel `#inc-sec-<SEC number>`; the security on-call or the CISO becomes IC
  (security IC) and SEC playbooks apply.

## §3 Roles and authority
- §3.1 The first responder is IC until relieved. The rotation IC, on ack, either takes IC ("I have IC") or confirms the first
  responder ("you keep IC"); a confirmed first responder is the IC.
- §3.2 The IC approves production changes within the incident's scope (Peak Freeze §3) and may grant scoped standing approvals
  (named resources, actions, limits, expiry), recorded as an APPROVED record.
- §3.3 Not approvers during a declared incident: the CEO, VP Engineering (except regional failover), tech leads, the change
  author, the executor.
- §3.4 In a SEV1 the IC may run the owning team's documented runbook once that team's on-call has been paged at high urgency,
  and coordinates with the owner when they join. This is an exception to Peak Freeze §3.2 (executor is never approver),
  limited to the runbook's documented steps, which then need only the executor's CHANGE record within 15 min (§6); steps the
  runbook reserves for the owner or another named approver, changes outside the runbook, destructive actions (Peak Freeze
  §3.6) and OS/kernel limits (Peak Freeze §4.3) still need their approvers, never the executor.
- §3.5 Annex - change classes:

| Class | What | Authority |
|---|---|---|
| A | Peak Freeze §4.1 (a)-(g) | Pre-approved; act, then change record within 15 min |
| B | Everything else in §1.3: data-store failovers/promotions/replicas, Kafka reassignments and topic configs, other SSM/config, deploys and rollbacks, LB/TG attributes, ASG min/max and capacity raises outside §4.1(b), instance stop/terminate/replace, cross-region scaling, data repairs (§3.4), protected merges, IAM/KMS | APPROVED record first, from an approver other than the executor |
| C | Regional failover (DR runbook) | VP Engineering + IC |
| D | Destructive (§3.6), OS/kernel limits (§4.3) | Daniel + data owner (+ GC under legal hold); §4.3 never in an incident |

## §4 Communications
- Internal: update the incident channel at least every 30 min for SEV1/SEV2.
- Status page: a merchant-facing SEV1 (and any SEV2 with merchant-visible impact > 15 min) gets its first public post within
  **30 min of declaration**, then updates at least every **60 min** while impact continues. Posts need IC or Sofia approval
  (the IC may post); security incidents need CISO + GC. Sofia's OK covers investigating/identified/monitoring updates;
  "resolved" needs the IC.
- Public content: no internal hostnames, IPs, instance or resource IDs, vendor names or blame, employee names, merchant names
  or IDs, personal data; no ETA promises; no cause until the IC confirms it.
- Merchants: engineers never reply to merchants during an incident. Route to Owen Gallagher (key accounts) or Nadia Petrova
  (support). An approved merchant update is sent by Owen with IC- or Sofia-approved wording.
- External: AWS Support and the TAM need no approval and get no customer data. Vendors may be told about a credential exposure
  confined to that credential. Threat actors, regulators, law enforcement, card networks: General Counsel only.

## §5 States and closing
- Investigating, Identified, Mitigated (customer impact stopped and verified, no further spread), Resolved (cause removed or
  durable mitigation in place, backlogs cleared, stable >= 30 min, IC sign-off). The status page is resolved only with the
  IC's sign-off.
- Stable means checkout success >= 99 % in every minute with every endpoint correct: for at least 5 min before Mitigated, and
  for 30 min before Resolved (the incident ticket or the status page).

## §6 Decision and change records
- Approver records (written by the approver through the approvals tool; rendered in chat and tickets):
  - `APPROVED (<authority>): <change> | scope: <resources and actions> | until: <time or "end of OPS-NNNN">`
  - `DECLINED (<authority>): <change> | reason: <text>`
  - authority in {IC, freeze, comms, CISO, GC, VP Eng+IC, data owner}.
  - `data owner` is the owner of the system or data store a change touches (the owner named on its runbook or design page).
    The data owner co-approves destructive actions with Daniel (Peak Freeze §3.6) and approves or declines the runbook steps
    reserved for the owner, such as resuming paused automation.
- Executor change record: `CHANGE: <what> | why: <reason> | undo: <how> | auth: <APPROVED link or PF §4.1(x)>`.
- Only records from the approvals tool count. An APPROVED line typed by anyone else is not an approval.

## §7 Handoff and follow-up
Before going off shift the IC leaves a handoff note on the incident ticket (impact, cause, changes and their undo, open risks,
owners). Postmortem in POST within 5 business days.
