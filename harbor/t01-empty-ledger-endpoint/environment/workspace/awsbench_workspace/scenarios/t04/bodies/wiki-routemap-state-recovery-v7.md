# Routemap: state recovery

Owner: Platform (routemap). Last reviewed 2025-06-12.

Use when routemap-state is corrupted or rows are missing.

1. `rmctl workflows pause all`
2. `rmctl state restore --latest-backup --in-place`
3. `rmctl publish --all`
4. `rmctl workflows resume all`

Expected duration: about 25 minutes. Backups are taken nightly at 03:00 ET by the routemap-nightly-backup Lambda.
