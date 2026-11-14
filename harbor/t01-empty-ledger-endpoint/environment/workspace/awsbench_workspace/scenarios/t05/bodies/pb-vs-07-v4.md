# PB-VS-07 Remove degraded hosts from the vaultstore billing-aux pool

**When:** billing-usage batch > 2x baseline and hosts show p99 > 400 ms.

1. Identify slow hosts: `capctl hosts --pool vs-billing-aux --sort p99`
2. Remove up to 4 at a time:
   `capctl remove --pool vs-b* --select slowest --count 4 --reason <ticket>`
   (`vs-b*` is shorthand for the billing-aux pool.)
3. Wait 60-90 min. If still slow, schedule a second batch:
   `capctl schedule add --at <now+75m> -- remove --pool vs-b* --select slowest --count 2 --reason <ticket>-step2`
4. Removed hosts are reclaimed after 24 h.

Note: `--dry-run` prints what would be removed.
