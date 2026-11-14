# PB-VS-07 Remove degraded hosts from the vaultstore billing-aux pool (v1)

When billing-usage runs > 2x baseline, find the slow hosts with `capctl hosts --pool vs-billing-aux --sort p99` and remove them with `capctl remove --pool vs-billing-aux --hosts <h1,h2>`.
