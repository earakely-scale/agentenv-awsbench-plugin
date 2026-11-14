# POST-2026-006 Routemap DR game day (2026-03-14)

Blameless review of a planned exercise.

**Scenario.** Simulated total loss of routemap-state in us-east-1.

**What we did.** Restored the nightly backup to `routemap-state-dr` with RestoreTableFromBackup, repointed `/routemap/prod/state_table` to it, ran `rmctl publish --all`. Time to recovery: 22 minutes.

**What we learned.**
- A repoint loses every write made since the backup (onboarding, merchant edits, ops changes). It is acceptable only for total regional loss.
- `publish --all` after a repoint pushes lower route_versions, which the data plane rejects as STALE_VERSION; merchants changed since the backup keep their newer config in the data plane until their next write.
- For partial corruption: point-in-time restore into a side table and a targeted merge. Kwame to write this up in the routemap repo (done: docs/state-recovery.md, PR #571).

Participants: Daniel Brandt (facilitator), Kwame Asante, Ravi Menon, Grace Liu.
