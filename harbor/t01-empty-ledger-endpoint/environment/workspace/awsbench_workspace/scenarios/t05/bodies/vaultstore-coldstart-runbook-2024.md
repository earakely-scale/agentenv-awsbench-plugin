# Runbook: vaultstore cold start

Author: Kwame Asante, 2024-05-20 (after the 2024-05-14 game day).

Fastest path: `capctl coldstart --subsystem all` starts index and placement together; placement uses its cached allocation map while index verifies.

Game day 2024-05-14: 11 min end to end.

If the check looks stuck for more than 5 minutes, `capctl coldstart --subsystem index --abort` and start again.
