rmctl maintenance subcommands (purge-orphans, reindex, compact) resolve the target environment from the caller's account when `--env` is omitted (`resolve_env()` in rmctl/maintenance/purge_orphans.py). The prod confirmation prompt only runs when `--env prod` is passed explicitly.

Asks:
1. Require `--env` for every maintenance subcommand; refuse to run without it.
2. Always confirm on prod (type the account alias).
3. Refuse a `--registry-snapshot` whose S3 bucket belongs to another environment.
4. Write a key manifest (every key touched, old and new values) on `--apply`, not only on `--dry-run`.

Risk if not done: a staging cleanup run with a prod profile exported tombstones prod routes.
