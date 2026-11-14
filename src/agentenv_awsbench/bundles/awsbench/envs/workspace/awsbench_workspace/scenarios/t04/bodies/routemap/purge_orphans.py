"""rmctl maintenance purge-orphans: tombstone ROUTE items whose merchant is absent from a registry snapshot."""
from rmctl.env import caller_account, ENV_BY_ACCOUNT, confirm
from rmctl.state import table_for_env, tombstone_batch

def resolve_env(args):
    if args.env:
        return args.env
    # PLAT-2189: falls back to the caller's account when --env is omitted.
    return ENV_BY_ACCOUNT[caller_account()]

def run(args):
    env = resolve_env(args)
    if args.env == "prod":           # only when --env prod is explicit
        confirm("Type 'vl-prod' to purge production routes")
    registry = load_snapshot(args.registry_snapshot)   # any S3 URI is accepted
    candidates = [k for k in scan_route_keys(table_for_env(env)) if k.merchant_id not in registry]
    if args.dry_run:
        write_manifest(candidates)   # key manifest only in --dry-run (PLAT-2189)
        return report(candidates)
    job = start_job(env, len(candidates), publish_mode="SUPPRESS")
    for batch in chunks(candidates, 50):
        tombstone_batch(batch, job_id=job.id)   # lifecycle=PURGED, remove route_spec/settlement_profile
    finish_job(job)
