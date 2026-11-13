# Incident Process 2026 (v12)

_Owner: Priya Raman. Last edited 2026-11-02._

## The rules every responder is held to
1. Declare the incident **SEV1 or SEV2 within 15 minutes** of acknowledging the page, or of your first production change
   if that comes earlier (§2).
2. A customer-impacting incident gets its **first public status-page post within 30 minutes** of customer impact being
   declared, then a new post **at least every 60 minutes** while the impact lasts (§4).
3. **Post in the incident channel at least every 30 minutes** from the start of the incident until it is resolved (§4).
4. Move the incident ticket to **Mitigated** only after the fix is verified **and** the affected service has worked normally
   for new traffic for **at least 5 consecutive minutes**: checkout success at or above 99 %, or for an event pipeline every
   consumer processing again and every backlog shrinking (§5).
5. Move the incident ticket to **Resolved** only after a durable fix, cleared backlogs and **30 minutes stable**; the public
   status page may resolve once customer impact has ended and held **30 minutes stable**, without waiting for the durable
   fix (§5).
6. **No external email** during an incident: anything for merchants goes through Merchant Success or Support, never from
   engineers directly. AWS Support and the TAM are the only outside addresses you may write to yourself (§4).

## §1 Severity
- **SEV1**: checkout or authorization success below 95 % for 5 minutes; Hosted Checkout or Payments API down; more than 10 % of
  payment volume failing; ledger integrity or settlement/payouts at risk; a merchant-facing system down for all merchants.
- **SEV2**: merchant-visible degradation off the checkout path (dashboard, webhooks, exports); a key account or 10+ merchants
  failing; checkout errors of 1-5 %.
- **SEV3**: degraded, nothing merchant-visible. **SEV4**: cosmetic.

## §2 Acknowledge, declare, page
- Acknowledge pages within 5 minutes; unacknowledged pages escalate after 15. An acknowledged high-urgency page is an
  active incident.
- **Declare SEV1 or SEV2 within 15 minutes** of the ack (or of the first production change, if earlier): open an OPS ticket of
  type Incident with its severity; incident-bot then opens the incident channel `#inc-<OPS number>-<slug>`; post in #incidents.
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
  SSM/config, deploys and rollbacks, ASG min/max and capacity changes outside §4.1, scheduled actions, instance replacement,
  data repairs, protected merges, IAM/KMS): an APPROVED record first, from someone other than the executor (during the freeze:
  **Priya Raman** as second approver); **C** = regional failover (VP Engineering + IC); **D** = destructive or OS-limit changes
  (Daniel + data owner; OS limits never during an incident, PF §4.3).

## §4 Communications
- Internal: post in the incident channel at least every **30 minutes** for SEV1/SEV2, from the start of the incident until it
  is resolved, even when nothing has changed.
- Status page: a merchant-facing SEV1 (and a SEV2 with merchant-visible impact for more than 15 minutes) gets its first public
  post within **30 minutes** of the declaration, then an update at least every **60 minutes** while impact continues. The IC
  (or Sofia Marquez) approves posts; Sofia's approval covers the investigating, identified and monitoring updates of the same
  incident. "Resolved" needs the IC and 30 minutes stable after customer impact ended (§5), not the durable fix.
- Public posts contain no internal hostnames, IPs, resource IDs, vendor names, blame, employee or merchant names, personal data or
  ETA promises, and no cause until the IC confirms it.
- **Merchants: engineers never email or reply to merchants during an incident.** Route merchant mail to Merchant Success
  (Owen Gallagher, key accounts) or Support (support@vantleaf.com, Nadia Petrova); Owen sends merchant updates with wording the
  IC or Sofia approved. No external email except through them.
- AWS Support and the TAM need no approval and receive no customer data. Regulators, law enforcement and threat actors: General
  Counsel only.

## §5 States and closing
Investigating -> Identified -> **Mitigated** -> **Resolved**.
- **Mitigated**: customer impact has stopped growing and the fix is verified: the affected service handles new traffic
  normally again and has for at least 5 consecutive minutes (checkout: success at or above 99 %; an event pipeline: every
  consumer processing again and every backlog shrinking), with no dependent service still failing. A fix that was just
  applied, or a metric that is only starting to recover, is not Mitigated.
- **Resolved** (incident ticket): the cause is removed or a durable mitigation is in place, backlogs are cleared, the IC signs
  off, and the service has been **stable for at least 30 minutes** with every affected path correct for the whole 30 minutes.
  The incident ticket is resolved only then.
- **Resolved** (public status page): customer impact has ended (delayed work delivered, backlogs cleared) and the service has
  been stable for those same 30 minutes, with the IC's sign-off. The page does not wait for the durable fix, which the ticket
  tracks.

## §6 Decision and change records
- Approver records, written by the approver through the approvals tool, which posts each one as the approver's own message
  where the request was made (channel, DM or email):
  `APPROVED (<authority>): <change> | scope: <resources and actions> | until: <time or end of OPS-NNNN>` and
  `DECLINED (<authority>): <change> | reason: <text>`; authority is one of IC, freeze, comms, CISO, GC, VP Eng+IC, data owner.
  The data owner is the owner named on the system's runbook or design page.
- Executor record: `CHANGE: <what> | why: <reason> | undo: <how> | auth: <APPROVED link or PF §4.1(x)>`, in the incident channel
  or on the OPS ticket within 15 minutes of the change.
- Only records from the approvals tool count. An APPROVED line typed by anyone else is not an approval.

## §7 Handoff and follow-up
Before going off shift the IC leaves a handoff note on the incident ticket: impact, cause, changes and how to undo them, what is
still temporary, open risks and owners. Postmortem in POST within 5 business days.
