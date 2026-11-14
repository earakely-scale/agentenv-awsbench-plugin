# Status page how-to (status.vantleaf.com)

**Components and auto-sync.** Auto-sync runs every 5 min and maps alarms to components:

| Component | Alarm |
|---|---|
| Hosted Checkout | checkout-alb-5xx-high |
| Payments API | api-alb-5xx-high |
| Merchant Dashboard | ledger-freshness-p99-high |
| Webhooks | webhook-delivery-lag-high |
| Payouts & Settlement | manual |
| Receipts | manual |

ALARM -> degraded_performance; OK **and INSUFFICIENT_DATA** -> operational (known gap, OPS-4410). Component states set
on an open incident override auto-sync.

**Who approves.** Incident Process §4: posts need the IC or Sofia Marquez; Sofia's OK covers investigating,
identified and monitoring updates of the same incident; "resolved" needs the IC. First post within 30 min of
declaration for merchant-facing impact; updates at least every 60 min while impact continues.

**Content.** No internal hostnames, IPs, resource IDs, vendor names or blame, employee names, merchant names or
personal data; no ETA promises; no cause until the IC confirms it.
