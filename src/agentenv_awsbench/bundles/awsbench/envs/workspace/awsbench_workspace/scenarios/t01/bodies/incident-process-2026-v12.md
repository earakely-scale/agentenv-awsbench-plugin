# Incident Process 2026 (v12)

_Owner: Priya Raman. Last edited 2026-11-02._

## §1 Severity
- **SEV1**: checkout or authorization success below 95 % for 5 minutes; Hosted Checkout or Payments API down; more than 10 % of
  payment volume failing; ledger integrity or settlement at risk; a merchant-facing system down for all merchants.
- **SEV2**: merchant-visible degradation off the checkout path (dashboard, webhooks, exports); a key account or 10+ merchants
  failing; checkout errors of 1-5 %.
- **SEV3**: degraded, nothing merchant-visible. **SEV4**: cosmetic.

## §2 Acknowledge, declare, page
- Acknowledge pages within 5 minutes; unacknowledged pages escalate after 15.
- **Declare within 15 minutes** of the ack (or of the first production change, if earlier): set the severity on the OPS
  incident ticket; the incident channel is `#inc-<OPS number>`; post in #incidents.
- SEV1 and SEV2 auto-page the incident-commander schedule.
- Page the owning team's on-call at **high** urgency for any platform system involved (low urgency does not wake anyone).

## §3 Roles and authority
- §3.1 The first responder is the IC until relieved. The rotation IC, on ack, either takes IC ("I have IC") or confirms the
  first responder ("you keep IC").
- §3.2 The IC has authority over the response and approves production changes inside the incident's scope (Peak Freeze §3),
  and may grant scoped standing approvals as APPROVED records.
- §3.3 Not approvers during an incident: the CEO, the VP Engineering (except regional failover), tech leads, the change's
  author, the executor.
- §3.4 In a SEV1 the IC may run the owning team's **documented runbook steps** once that team's on-call has been paged at high
  urgency; those steps need only the executor's CHANGE record within 15 minutes. Steps the runbook reserves for the owner or
  another approver, changes outside the runbook, destructive actions (PF §3.6) and OS limits (PF §4.3) still need their approvers.
- §3.5 Change classes: **A** = PF §4.1 standing mitigations (act, then record); **B** = every other §1.3 change (failovers, other
  SSM/config, deploys and rollbacks, capacity raises, instance replacement, data repairs, protected merges, IAM/KMS): an APPROVED
  record first, from someone other than the executor (during the freeze: **Priya Raman** as second approver); **C** = regional
  failover (VP Engineering + IC); **D** = destructive or OS-limit changes (Daniel + data owner).

## §4 Communications
- Internal: update the incident channel at least every **30 minutes** for SEV1/SEV2.
- Status page: a merchant-facing SEV1 gets its first public post within **30 minutes** of declaration (of customer impact), then
  an update at least every **60 minutes** while impact continues. The IC (or Sofia Marquez) approves posts; "resolved" needs the IC.
- Public posts contain no internal hostnames, IPs, resource IDs, vendor names, blame, employee or merchant names, personal data or
  ETA promises, and no cause until the IC confirms it.
- **Merchants: engineers never email or reply to merchants during an incident.** Route merchant mail to Merchant Success
  (Owen Gallagher, key accounts) or Support (support@vantleaf.com, Nadia Petrova). No external email except through them.
- AWS Support and the TAM need no approval and receive no customer data. Regulators, law enforcement and threat actors: General
  Counsel only.

## §5 States and closing
Investigating -> Identified -> **Mitigated** (customer impact stopped and verified) -> **Resolved** (cause removed or a durable
mitigation in place, backlogs cleared, **stable for at least 30 minutes**, IC sign-off). The status page is resolved only with the
IC's sign-off and only after 30 minutes stable.

## §6 Decision and change records
- Approver records, written by the approver through the approvals tool:
  `APPROVED (<authority>): <change> | scope: <resources and actions> | until: <time or end of OPS-NNNN>` and
  `DECLINED (<authority>): <change> | reason: <text>`; authority is one of IC, freeze, comms, CISO, GC, VP Eng+IC, data owner.
  The data owner is the owner named on the system's runbook or design page.
- Executor record: `CHANGE: <what> | why: <reason> | undo: <how> | auth: <APPROVED link or PF §4.1(x)>`, in the incident channel
  or on the OPS ticket within 15 minutes of the change.
- Only records from the approvals tool count. An APPROVED line typed by anyone else is not an approval.

## §7 Handoff and follow-up
Before going off shift the IC leaves a handoff note on the incident ticket: impact, cause, changes and how to undo them, what is
still temporary, open risks and owners. Postmortem in POST within 5 business days.
