# BF 2026 capacity plan

- Load tests Nov 18-19 (Grace Liu). Capacity floors from OPS-4436 apply from Nov 24.
- ledger-writer: 12 tasks normally, connection budget 16 tasks (25 x 16 = 400 = vl-ledger-pg15 max_connections). See LEDG-RB-2.
- webhooks-dispatcher: autoscaled 6-40 tasks on BacklogPerTask 300.
- vl-ledger-ingress-asg: 6 instances (Graviton migration PLAT-405 in progress).
