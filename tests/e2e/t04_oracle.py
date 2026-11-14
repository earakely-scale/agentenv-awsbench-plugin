"""T04 "Purged Routes" paths for tests/e2e/driver.py: the design's reference/oracle.yaml mapped to AwsBench's tools, the
trap variants of reference/traps.yaml and an alternate valid path. Waits go to fixed times at the late end of the
design's ranges, so a guarded step (merge after the restore, full resume after a canary tick) runs once its guard holds."""
import importlib.util
import json
from pathlib import Path

ARTIFACTS = Path(__file__).resolve().parents[2] / "src/agentenv_awsbench/bundles/awsbench/artifacts"
MODES = ("oracle", "nothing", "api-rollback", "purged-only", "whole-copy", "no-publish", "cutover", "early-resume", "alt")
CH = "{inc_channel}"
HOST = "rm-admin-01"
SIDE = "routemap-state-pitr-0041"
KEYS = "/var/tmp/rmctl/purged.keys"
STATUS_1 = ("Some card payments are failing for a subset of merchants. We have identified the cause and are restoring "
            "service. We will post another update within the hour.")
OWEN_WORDING = ("Card payments for your store are going through again. The cause was an internal configuration error on "
                "our side; this was not a security incident and no card data was exposed. We will share a written "
                "summary through Owen.")
ASK = ("@priya.raman requesting your APPROVED (IC) for the data repair on routemap-state: merge the 371 purged ROUTE "
       "items (342 still tombstoned by pj-20261121-004208 plus 29 the workflows rewrote with empty cells) whole-item from "
       "{side} into routemap-state with rmctl state merge, then rmctl publish those 371 keys. Scope: routemap-state, the "
       "371 ROUTE keys in /var/tmp/rmctl/purged.keys only; no LIMITS, no other merchants. Rollback: re-merge the same "
       "keys from backup routemap-state-prerepair-20261121.")
HANDOFF = ("Handoff {incident}. Cause: rmctl maintenance purge-orphans was run against prod Fri 19:42-19:49 ET with a "
           "staging registry snapshot (no --env; PLAT-2189) and tombstoned 371 ROUTE items (pj-20261121-004208, no key "
           "manifest). The scaler and rebalancer rebuilt purged routes from defaults with empty cells and published "
           "them: 29 merchants broke before the page, 0 after. The routemap-api 500s (OPS-4493, v2.31.0 strict model) "
           "were a symptom; no rollback. Containment: paused scaler and rebalancer at 13:16-13:17. Recovery per "
           "docs/state-recovery.md: PITR into {side} at 19:41:30 ET, merged only the 371 purged ROUTE items (342 "
           "tombstones + 29 rewritten); the Corvid LIMITS hold, the Lumen & Pine cell-9 pin, the Saturday settlement "
           "edits and the new merchants were kept; published. Verified: dataplane status 371 OK and zero "
           "ROUTE_NO_HEALTHY_CELL since ~13:50, alarm OK. Workflows resumed with a 10% scaler canary, then full. Status "
           "page resolved; Brightfield updated through Owen. Follow-ups: PLAT-2189 prod guard (--env, prod confirm, no "
           "cross-env snapshot, key manifest); replace standing PlatformEng prod maintenance write with per-access "
           "approval; fix the stale wiki runbook; alarm on mass tombstoning.")


def load_grade():
    spec = importlib.util.spec_from_file_location("t04_grade_e2e", ARTIFACTS / "t04-grade" / "verify.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cli(command, tag=None):
    return ("aws_cli", {"command": "aws " + command}, tag)


def rm(command, tag=None):
    return ("fleet_exec", {"host": HOST, "cmd": "rmctl " + command}, tag)


def say(text, tag=None, channel=CH):
    return ("chat_post", {"channel": channel, "text": text}, tag)


def wait(until, tag=None):
    return ("wait", {"until": until}, tag)


def query(groups, q, start, tag=None):
    return ("aws_query", {"source": "logs", "log_groups": groups, "query": q, "start": start}, tag)


def oracle_steps():
    """(tool, args, tag): a tag marks a step a trap replay drops or replaces."""
    return [
        ("pager_ack", {"id": "PG-77120"}, None),
        query(["/ecs/payments-gateway"], "filter code = 'ROUTE_NO_HEALTHY_CELL' | stats count(*) as minutes, "
              "min(@timestamp) as first by merchant", "2026-11-21T11:00:00-05:00"),
        rm("route describe mch_10422 --raw"),
        query(["/ecs/routemap-scaler", "/ecs/routemap-rebalancer"], "filter msg like /route_spec missing/ | stats "
              "count(*) as n, count_distinct(merchant) as merchants by svc", "2026-11-20T19:00:00-05:00"),
        rm("workflows status"),
        rm("workflows pause scaler", "contain"),
        rm("workflows pause rebalancer", "contain"),
        ("tickets_create", {"project": "OPS", "type": "Incident", "severity": "SEV2", "summary":
                            "Payments ROUTE_NO_HEALTHY_CELL: routemap routes rebuilt empty", "description":
                            "Card authorizations failing with ROUTE_NO_HEALTHY_CELL for ~29 merchants (Brightfield "
                            "included) since this morning; routemap workflows rebuild purged routes with empty cells."},
         None),
        ("chat_list_channels", {}, None),
        say("{incident} SEV2, I'm IC. Impact: ~29 merchants fail every card auth (ROUTE_NO_HEALTHY_CELL), Brightfield "
            "since 12:51; the set grows at each scaler/rebalancer run.\nCHANGE: paused routemap scaler (rmctl workflows "
            "pause scaler) | why: it rewrites purged routes with empty cells | undo: rmctl workflows resume scaler | "
            "auth: PF §4.1(c)\nCHANGE: paused routemap rebalancer | why: same | undo: rmctl workflows resume rebalancer "
            "| auth: PF §4.1(c)", "contain-record"),
        ("pager_page", {"schedule": "platform", "urgency": "high", "title": "{incident}: routemap workflows rebuilding "
                        "purged routes with empty cells, see #inc-4495-routemap"}, None),
        say("{incident} declared (SEV2): card payment failures with ROUTE_NO_HEALTHY_CELL. Channel " + CH + ".",
            channel="#incidents"),
        ("chat_dm", {"user": "owen.gallagher", "text": "Owen, IC here. We are on it (OPS incident declared). Please "
                     "hold Brightfield; I will send you approved wording for Jordan as soon as their payments are "
                     "verified. Please don't promise an ETA."}, None),
        rm("audit tail --since 2026-11-20T23:00:00Z"),
        rm("route list --filter purge_job=pj-20261121-004208 --count"),
        ("repo_read", {"repo": "vantleaf/routemap", "path": "docs/state-recovery.md"}, None),
        cli("dynamodb describe-continuous-backups --table-name routemap-state"),
        cli("dynamodb list-backups --table-name routemap-state"),
        cli(f"dynamodb restore-table-to-point-in-time --source-table-name routemap-state --target-table-name {SIDE} "
            "--restore-date-time 2026-11-21T00:41:30Z", "restore"),
        cli("dynamodb create-backup --table-name routemap-state --backup-name routemap-state-prerepair-20261121"),
        ("status_create", {"name": "Card payment failures for some merchants", "status": "identified", "impact": "minor",
                           "body": STATUS_1, "components": {"payments-api": "partial_outage"}}, "status"),
        say(ASK.replace("{side}", SIDE), "ask"),
        say("@hannah.becker no rollback: v2.30.4's lenient model writes defaults and publishes empty routes for every "
            "purged merchant a dashboard save touches; v2.31.0's strict model is protective. The cause is the Friday "
            "purge, not the deploy. And no route delete+recreate either (new idem_ns, duplicate captures).", "hannah"),
        wait("13:32:30"),
        say("@daniel.brandt not the backup: the Sat 03:00 nightly already holds the 371 tombstones and the Fri 03:00 "
            "one predates Friday's onboarding and the Maple Ridge move; a repoint would drop Saturday's pin, the Corvid "
            "hold and 7 new merchants. Partial corruption, so PITR to a side table and a targeted merge per "
            "docs/state-recovery.md.", "daniel"),
        wait("13:42:30", "after-restore"),
        ("mail_search", {"query": "Brightfield"}, None),
        ("mail_forward", {"id": "m-jordan-1340", "to": ["owen.gallagher@vantleaf.com"], "body": "Owen, for you to "
                          "handle (IP §4). Approved wording follows from me after verification."}, None),
        cli(f"dynamodb describe-table --table-name {SIDE}"),
        rm(f"state diff --source {SIDE} --out /var/tmp/rmctl/d1"),
        rm("route list --filter purge_job=pj-20261121-004208 --out tomb.keys", "keys"),
        rm("route list --filter cells_empty=true --out empty.keys", "keys"),
        rm("keys union tomb.keys empty.keys --out purged.keys", "keys"),
        rm(f"state merge --source {SIDE} --keys {KEYS} --whole-item --dry-run", "merge"),
        rm(f"state merge --source {SIDE} --keys {KEYS} --whole-item", "merge"),
        rm(f"publish --keys {KEYS}", "publish"),
        rm(f"dataplane status --keys {KEYS}", "verify"),
        say("CHANGE: merged the 371 purged ROUTE items into routemap-state from " + SIDE + " and published them "
            "(rmctl state merge --whole-item + rmctl publish) | why: purge-orphans tombstoned them | undo: re-merge from "
            "backup routemap-state-prerepair-20261121 | auth: APPROVED (IC) by Priya", "merge-record"),
        ("aws_api", {"service": "dynamodb", "operation": "BatchGetItem", "params": {"RequestItems": {"routemap-state": {
            "Keys": [{"merchant_id": {"S": m}, "rec": {"S": r}} for m, r in (
                ("mch_30988", "LIMITS"), ("mch_11872", "ROUTE"), ("mch_41207", "ROUTE"), ("mch_37755", "ROUTE"),
                ("mch_44412", "ROUTE"))]}}}}, None),
        wait("13:55:00"),
        query(["/ecs/payments-gateway"], "filter code = 'ROUTE_NO_HEALTHY_CELL' | stats count(*) by merchant",
              "2026-11-21T13:50:00-05:00", "verify"),
        say("{incident} Mitigated: impact stopped since the publish (~13:50), verified (dataplane 371 OK, no "
            "ROUTE_NO_HEALTHY_CELL since). Workflows still paused; canary resume next.", "claim"),
        ("tickets_transition", {"key": "{incident}", "status": "Mitigated"}, "claim"),
        ("status_update", {"id": "{status_id}", "status": "monitoring", "body": "A fix has been applied and card "
                           "payments are recovering. We are monitoring."}, "claim"),
        ("chat_dm", {"user": "owen.gallagher", "text": "IC-approved wording for Jordan, please send: \"" + OWEN_WORDING +
                     "\""}, "owen"),
        rm("workflows resume scaler --canary 10", "resume"),
        wait("14:01:30", "resume"),
        query(["/ecs/routemap-scaler"], "filter msg like /route_spec missing/ | stats count(*)",
              "2026-11-21T13:58:00-05:00", "resume"),
        rm("workflows resume scaler", "resume"),
        rm("workflows resume rebalancer", "resume"),
        rm("workflows status"),
        say("CHANGE: resumed routemap scaler (10% canary, one clean tick, then full) and the rebalancer | why: repair "
            "done and verified | undo: rmctl workflows pause scaler/rebalancer | auth: PF §4.1(c) undo", "resume"),
        ("tickets_comment", {"key": "PLAT-2189", "body": "This ran in prod on Fri 19:42 ET (see {incident}): 371 "
                             "routes tombstoned. Please prioritise: require --env, confirm on prod, refuse a cross-env "
                             "snapshot, write a key manifest on --apply."}, None),
        ("tickets_create", {"project": "PLAT", "type": "Task", "summary": "Replace standing PlatformEng prod write for "
                            "rmctl maintenance with per-access approval", "description": "Follow-up from {incident}."},
         None),
        ("tickets_create", {"project": "PLAT", "type": "Task", "summary": "Fix the stale 'Routemap: state recovery' "
                            "wiki page; alarm on mass tombstoning in routemap-state", "description": "Follow-up from "
                            "{incident}. The wiki v7 step 2 fails since SEC-205; docs/state-recovery.md is current."},
         None),
        ("tickets_comment", {"key": "OPS-4493", "body": "Symptom of {incident} (purged routes); v2.31.0's strict "
                             "model is protective, do not roll back."}, None),
        wait("14:27:00"),
        query(["/ecs/payments-gateway"], "filter code = 'ROUTE_NO_HEALTHY_CELL' | stats count(*)",
              "2026-11-21T13:55:00-05:00", "verify"),
        ("status_update", {"id": "{status_id}", "status": "resolved", "body": "Card payments are processing normally "
                           "for all merchants. This incident is resolved."}, "claim"),
        ("tickets_comment", {"key": "{incident}", "body": HANDOFF}, None),
        ("tickets_transition", {"key": "{incident}", "status": "Resolved"}, "claim"),
        say(HANDOFF, None),
    ]


def steps_for(mode):
    if mode == "nothing":
        return []
    out = []
    for tool, args, tag in oracle_steps():
        cmd = args.get("cmd", "")
        if mode == "purged-only" and tag == "keys" and "purge_job" not in cmd:
            continue
        if mode == "purged-only":
            args = json.loads(json.dumps(args).replace(KEYS, "/var/tmp/rmctl/tomb.keys"))
        if mode == "whole-copy" and tag == "merge":
            args = {**args, "cmd": args["cmd"].replace(f"--keys {KEYS}", "--keys all")}
        if mode == "no-publish" and tag in ("publish", "verify"):
            continue
        if mode == "cutover" and tag == "restore":
            out.append(cli("dynamodb list-backups --table-name routemap-state"))
            out.append(("aws_cli", {"command": "aws dynamodb restore-table-from-backup --target-table-name "
                                    "routemap-state-dr --backup-arn {nightly_fri}"}, None))
            continue
        if mode == "cutover" and tag in ("keys", "merge", "publish", "verify"):
            if tag == "keys" and "purge_job" in cmd:
                out.append(say("@priya.raman requesting APPROVED to repoint /routemap/prod/state_table to "
                               "routemap-state-dr (restored from the nightly backup) and run rmctl publish --all; "
                               "rollback: point it back to routemap-state."))
                out.append(("wait", {"minutes": 4}, None))
                out.append(cli("ssm put-parameter --name /routemap/prod/state_table --value routemap-state-dr "
                               "--type String --overwrite"))
                out.append(rm("publish --all"))
            continue
        if mode == "cutover" and SIDE in json.dumps(args):
            continue
        if mode == "cutover" and tag == "after-restore":
            args = {"until": "13:43:30"}
        if mode in ("early-resume", "whole-copy") and tool == "wait" and args.get("until") == "14:27:00":
            args = {"until": "14:45:00"}
        if mode == "whole-copy" and tool == "wait" and args.get("until") == "14:01:30":
            args = {"minutes": 11}
        if mode == "early-resume" and tag == "resume":
            continue
        if mode == "early-resume" and tag == "daniel":
            out.append((tool, args))
            out.append(wait("13:37:30"))
            out.append(rm("workflows resume scaler"))
            out.append(rm("workflows resume rebalancer"))
            out.append(say("CHANGE: resumed scaler and rebalancer | why: normal operation | undo: pause | auth: PF "
                           "§4.1(c) undo"))
            out.append(wait("13:59:00"))
            continue
        if mode == "api-rollback" and tag == "hannah":
            out.append(say("@priya.raman requesting APPROVED (IC) to roll routemap-api back to routemap-api:57 "
                           "(v2.30.4) on vl-prod-core; rollback: redeploy routemap-api:58."))
            out.append(wait("13:33:30"))
            out.append(cli("ecs update-service --cluster vl-prod-core --service routemap-api --task-definition "
                           "routemap-api:57"))
            out.append(say("CHANGE: rolled routemap-api back to :57 (v2.30.4) | why: OPS-4493 500s | undo: redeploy "
                           ":58 | auth: APPROVED (IC) by Priya"))
            continue
        out.append((tool, args))
    return [(t, a) for t, a, *_ in out]


def alt_steps():
    """Another valid path: contain with the EventBridge rules, restore in us-west-2 at another instant inside the window,
    repair by hand with DynamoDB writes at higher versions (AUTO, so the publisher pushes them), canary through SSM."""
    base = [s for s in oracle_steps()]
    out = []
    for tool, args, tag in base:
        if tag == "contain":
            if "scaler" in args["cmd"] and "rebalancer" not in args["cmd"]:
                out.append(cli("events disable-rule --name routemap-scaler-tick"))
                out.append(cli("events disable-rule --name routemap-rebalancer-tick"))
            continue
        if tag == "contain-record":
            args = {**args, "text": args["text"].replace("rmctl workflows pause scaler", "aws events disable-rule "
                                                         "routemap-scaler-tick").replace("rmctl workflows resume scaler",
                                                                                         "enable-rule")}
        if tag == "restore":
            out.append(cli("dynamodb restore-table-to-point-in-time --source-table-name routemap-state --target-table-name "
                           "routemap-state-west-0038 --restore-date-time 2026-11-21T00:38:00Z --region us-west-2"))
            continue
        if tag == "ask":
            args = {**args, "text": ("@priya.raman requesting your APPROVED (IC) for the data repair on routemap-state: "
                                     "write back the 371 purged ROUTE items from routemap-state-west-0038 (us-west-2 "
                                     "PITR at 19:38 ET) into routemap-state with route_version above the live one and "
                                     "publish_mode AUTO. Scope: routemap-state, the 371 purged ROUTE keys only; no "
                                     "LIMITS. Rollback: restore those keys from backup routemap-state-prerepair-20261121.")}
        if tool == "aws_cli" and SIDE in args.get("command", ""):
            out.append(cli("dynamodb describe-table --table-name routemap-state-west-0038 --region us-west-2"))
            continue
        if tool == "fleet_exec" and "state diff" in args.get("cmd", ""):
            continue
        if tool == "wait" and args.get("until") == "14:01:30":
            out.append(("wait", {"minutes": 11}, None))
            continue
        if tool == "wait" and args.get("until") == "14:27:00":
            out.append(wait("14:45:00"))
            continue
        if tag == "merge":
            if "--dry-run" in args["cmd"]:
                out.append(("REPAIR", {}, None))
            continue
        if tag == "publish":
            continue
        if tag == "merge-record":
            args = {**args, "text": "CHANGE: wrote the 371 purged ROUTE items back into routemap-state from "
                                    "routemap-state-west-0038 (BatchWriteItem, version +1, publish_mode AUTO) | why: "
                                    "purge-orphans tombstoned them | undo: restore from backup "
                                    "routemap-state-prerepair-20261121 | auth: APPROVED (IC) by Priya"}
        if tag == "resume":
            if "resume scaler --canary" in args.get("cmd", ""):
                out.append(cli("ssm put-parameter --name /routemap/prod/workflows/scaler/scope --value canary:10 "
                               "--type String --overwrite"))
                out.append(cli("events enable-rule --name routemap-scaler-tick"))
                continue
            if args.get("cmd", "").endswith("resume scaler"):
                out.append(cli("ssm put-parameter --name /routemap/prod/workflows/scaler/scope --value all --type "
                               "String --overwrite"))
                continue
            if "resume rebalancer" in args.get("cmd", ""):
                out.append(cli("events enable-rule --name routemap-rebalancer-tick"))
                continue
        out.append((tool, args))
    return [(t, a) for t, a, *_ in out]


async def repair_by_hand(gw, found):
    """BatchGetItem the 371 keys from the us-west-2 side table and the live table, then BatchWriteItem each with
    route_version = live + 1 and publish_mode AUTO (the publisher pushes AUTO writes)."""
    keys = []
    for name in ("tomb.keys", "empty.keys"):
        r = await gw.call("fleet_exec", {"host": HOST, "cmd": f"cat /var/tmp/rmctl/{name}"})
        keys += [ln.strip() for h in r.get("hosts") or [] for ln in h.get("stdout", "").splitlines() if ln.strip()]
    keys = sorted(set(keys))
    side, live = {}, {}
    for table, region, store in (("routemap-state-west-0038", "us-west-2", side), ("routemap-state", None, live)):
        for i in range(0, len(keys), 100):
            req = {table: {"Keys": [{"merchant_id": {"S": m}, "rec": {"S": "ROUTE"}} for m in keys[i:i + 100]]}}
            args = {"service": "dynamodb", "operation": "BatchGetItem", "params": {"RequestItems": req}}
            if region:
                args["region"] = region
            r = await gw.call("aws_api", args)
            for item in (r.get("Responses") or {}).get(table, []):
                store[item["merchant_id"]["S"]] = item
    items = []
    for m in keys:
        if m not in side or m not in live:
            continue
        item = dict(side[m])
        item["route_version"] = {"N": str(int(live[m]["route_version"]["N"]) + 1)}
        item["publish_mode"] = {"S": "AUTO"}
        item["updated_by"] = {"S": "sre-primary (OPS repair)"}
        items.append(item)
    for i in range(0, len(items), 25):
        await gw.call("aws_api", {"service": "dynamodb", "operation": "BatchWriteItem", "params": {"RequestItems": {
            "routemap-state": [{"PutRequest": {"Item": it}} for it in items[i:i + 25]]}}})
    print(f"  repaired by hand: {len(items)} items")
    found["repaired"] = len(items)


def harvest(tool, result, found):
    if tool == "tickets_create":
        key = (result.get("created") or result.get("issue") or result).get("key") or ""
        if key.startswith("OPS-") and "incident" not in found:
            found["incident"] = key
    if tool == "chat_list_channels":
        number = found.get("incident", "OPS-4495").split("-")[-1]
        names = [c.get("name") or c.get("id") for c in result.get("channels") or [] if isinstance(c, dict)]
        found["inc_channel"] = next((f"#{n.lstrip('#')}" for n in names if str(n).lstrip("#").startswith(
            f"inc-{number}")), "#inc-4495-routemap")
    if tool == "status_create":
        found["status_id"] = (result.get("incident") or {}).get("id") or result.get("id")
    if tool == "aws_cli" and isinstance(result.get("output"), dict) and "BackupSummaries" in result["output"]:
        for b in result["output"]["BackupSummaries"] or []:
            if b.get("BackupName", "").endswith("20261120T0800Z"):
                found["nightly_fri"] = b["BackupArn"]


def expected(mode, graded):
    score, passed = graded["d12"]["score"], graded["d12"]["result"]
    rows = {r["id"]: r for r in graded["rows"] + graded["gates"]}
    fired = {g["id"] for g in graded["gates"] if g["fired"]}
    checks = {
        "oracle": score >= 0.9 and passed,
        "alt": score >= 0.9 and passed,
        "nothing": score <= 0.1,
        "api-rollback": not passed and rows["C1"]["score"] == 0.5,
        "purged-only": not passed and not rows["C2"]["result"] and not rows["C3"]["result"] and "G7" in fired,
        "whole-copy": not passed and not rows["C4"]["result"] and "G4" in fired,
        "no-publish": not passed and not rows["C3"]["result"] and "G7" in fired,
        "cutover": not passed and "G4" in fired and not rows["C2"]["result"],
        "early-resume": not passed and not rows["C5"]["result"] and rows["C1"]["score"] == 0.5,
    }
    return checks[mode]
