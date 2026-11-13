# EPD-RB-7 endpointd: endpoint empty or enactors halted

**Who:** platform on-call. In a SEV1, the incident commander (or acting IC, Peak Freeze §3.3) once platform has been paged
at **high** urgency (Incident Process §3.4). Steps 1-5 and 7 are this runbook's documented steps: act, then post a
`CHANGE:` record (Incident Process §6) within 15 min. Step 6 needs an `APPROVED` record from Kwame Asante or Daniel Brandt.
Deleting plan items, archive objects or other endpoints' records is destructive (Peak Freeze §3.6).

1. Pause automation before touching anything: `epctl pause all` (sets the four kill switches to false; Peak Freeze §4.1(c)).
2. Run `epctl status` and `epctl verify` to list endpoints whose live records differ from their applied plan.
3. If an endpoint's records are missing, re-apply the endpoint's applied plan:
   `epctl apply --gen <applied gen> --endpoint <fqdn> --manual --reason <ticket>`.
   If the plan item is gone, run `epctl plan restore --gen <gen> --from-archive` first.
   Before applying any plan by hand, check it against the current fleet: `epctl plan diff <gen> --live-fleet --endpoint <fqdn>`.
   (By hand: write exactly the plan's records for the endpoint with `route53 change-resource-record-sets`, then `epctl set-applied`.)
4. Verify from inside the VPCs with `epctl verify --resolve` or `dig` run on a host in each AZ (at least two AZs), and do it
   before any public `monitoring` or `resolved` status post and before moving the incident ticket to Mitigated or Resolved.
   `dig @<resolver>` asks the resolver, not the hosts' own resolution, so it verifies nothing for clients. Route 53
   TestDNSAnswer does not work for private zones. Clients may keep a negative answer for up to the zone's SOA negative TTL.
5. While endpointd is paused, endpoints do not follow fleet changes: hold membership changes (instance refresh, scale-in,
   replacements) for fleets behind any endpoint applied by hand, or re-apply after each change.
6. Do not resume automation until the endpointd owner (Kwame Asante) or Daniel Brandt records `APPROVED` in the incident channel.
7. Leave the plan store consistent: every `applied#` pointer must reference an existing plan that matches the live records (`epctl verify` OK).

_v6, 2026-11-03, Daniel Brandt: "Who" section aligned with Incident Process v12, and step 4 says what verification must come
before (runbook sweep). Other steps unchanged since v5 (2025-08-14, Kwame Asante)._
