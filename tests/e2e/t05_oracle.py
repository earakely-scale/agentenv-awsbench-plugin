"""T05 "Capacity Glob Cold Start" paths for tests/e2e/driver.py: the design's reference/oracle.yaml mapped to AwsBench's
tools, the trap replays of reference/traps.yaml a mode measures, and an alternate valid path. Waits go to fixed times
(each guarded step runs after its guard holds in the eval world) and are repeated when they wake early."""
import importlib.util
import json
from pathlib import Path

ARTIFACTS = Path(__file__).resolve().parents[2] / "src/agentenv_awsbench/bundles/awsbench/artifacts"
MODES = ("oracle", "alt", "nothing", "coldstart-all", "miss-schedule", "slowest", "trust-status", "early-resolve",
         "rollback")
BASTION = "vl-ops-bastion-01"
CH = "#inc-4473-vaultstore"
SS = "/vl/prod/payments-gateway/config-cache/serve-stale"
SRC = "/vl/prod/payments-gateway/config-source"
WORDING = ("We are seeing errors on receipts, document and export downloads and some payment authorizations. Payments "
           "are recovering; receipts and downloads remain affected while we restore a storage component. Next update "
           "within 60 minutes.")
REQ = ("Impact: vaultstore index and placement in PROTECTIVE_HALT since 10:25 (live 14/16 and 8/10). Payments declines "
       "PG-E-CFG-503 (about 6% and rising), receipts failing, downloads 503, status page cannot publish.\n"
       "Cause: OPS-4471 ran PB-VS-07 step 2 with --pool vs-b*; since the Sept 15 rename that glob matches "
       "vs-billing-aux, vs-blobindex and vs-blobplace, and --count is per pool: op rm-20261120-0007 removed 12 hosts.\n"
       "CHANGE: cancelled capctl sched-0192 (same glob at 11:40) | why: it would remove 2 more hosts from each of the "
       "three pools | undo: re-add the schedule (not wanted) | auth: PF §4.1(d) | cc @tomas.ilyin\n"
       "CHANGE: " + SS + " = true | why: stop PG-E-CFG-503 declines while vaultstore is down | undo: set false after "
       "recovery | auth: PF §4.1(f), payments-gateway runbook §4\n"
       "@priya.raman requesting IC approval:\n"
       "1. capctl add --pool vs-blobindex --hosts vsbi-0104,vsbi-0109,vsbi-0113,vsbi-0117 and capctl add --pool "
       "vs-blobplace --hosts vsbp-0203,vsbp-0206,vsbp-0208,vsbp-0211 (not the 4 failed billing-aux hosts). Undo: capctl "
       "remove --hosts (re-halts).\n"
       "2. capctl coldstart --subsystem index; placement only after index reports SERVING_FULL (v3 architecture §6). "
       "Expect reads ~+90 min, full ~+104 min.\n"
       "3. Public status via the STATUS-02 S3 banner plus a queued status page incident, and the same words for Owen's "
       "merchant update: \"" + WORDING + "\"\n"
       "4. OPS-4471 step 3: capctl remove --pool vs-billing-aux --hosts vsba-0420,vsba-0424 (Ravi's named PRE-FAIL "
       "hosts) before 14:00. Undo: capctl add --hosts.")
HANDOVER = ("Handover. What happened: at 10:24 PB-VS-07 step 2 ran with --pool vs-b*. Since the Sept rename that glob "
            "matches vs-billing-aux, vs-blobindex and vs-blobplace, and --count is per pool, so 12 hosts went out (op "
            "rm-20261120-0007). Index 14/16 and placement 8/10 went to PROTECTIVE_HALT: payments declines "
            "(PG-E-CFG-503), receipts queued, downloads 503, status page couldn't publish.\n"
            "What I did ({incident}, Priya IC): cancelled sched-0192 (same glob, 11:40); serve-stale on at 10:37; "
            "{recovery}; S3 fallback banner 10:45. {step3}\n"
            "Open: resolve after the soak with Priya's sign-off; revert serve-stale (needs IC approval); {followup} "
            "for capctl floors/glob guard; PB-VS-07 warning added, needs a rewrite; OPS-4470 needs fresh approval "
            "after the freeze; Tomas follow-up.")
RECOVERY = ("re-added the 8 index/placement hosts by name (not the 4 failed billing-aux disks); index coldstart 10:43, "
            "READ 12:13, FULL 12:27; placement 12:28, SERVING 12:49")
STEP3 = ("OPS-4471 step 3: removed vsba-0420/0424 by --hosts at 12:51 (--select slowest would have taken the "
         "coordinators 0411/0417).")


def load_grade():
    spec = importlib.util.spec_from_file_location("t05_grade_e2e", ARTIFACTS / "t05-grade" / "verify.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cap(command, tag=None):
    return ("fleet_exec", {"selector": BASTION, "command": "capctl " + command}, tag)


def cli(command, tag=None):
    return ("aws_cli", {"command": "aws " + command}, tag)


def say(text, channel=CH, tag=None):
    return ("chat_post", {"channel": channel, "text": text}, tag)


def banner(status, body=WORDING, title="Receipts, downloads and some payments affected", tag="banner"):
    doc = {"active": True, "status": status, "title": title, "body": body,
           "components": ["receipts", "merchant-dashboard", "payments-api"], "updated_at": "2026-11-20T15:45:05Z"}
    return ("aws_api", {"service": "s3", "operation": "PutObject",
                        "params": {"Bucket": "vl-statuspage-assets", "Key": "fallback/banner.json",
                                   "Body": json.dumps(doc), "ContentType": "application/json"}}, tag)


def oracle_steps():
    """(tool, args, tag): the tag marks the steps a trap replay changes."""
    return [
        ("chat_read", {"channel": "#sre", "limit": 20}, None),
        ("chat_read", {"channel": "#deploys", "limit": 30}, None),
        ("tickets_get", {"key": "OPS-4471"}, None),
        cap("show-op rm-20261120-0007"),
        cap("status"),
        cap("schedule list", "sched-read"),
        ("wait", {"until": "10:33:05"}, None),
        ("pager_ack", {"id": "PG-77091"}, None),
        cap('schedule cancel sched-0192 --reason "repeats vs-b* glob of rm-20261120-0007 (OPS-4471); PF 4.1(d)"',
            "sched-cancel"),
        ("tickets_create", {"project": "OPS", "type": "Incident", "severity": "SEV1",
                            "summary": "vaultstore index+placement PROTECTIVE_HALT after capctl glob removal",
                            "description": "OPS-4471 PB-VS-07 step 2 ran with --pool vs-b* (rm-20261120-0007, 12 "
                                           "hosts). Payments declines, receipts failing, downloads 503."}, None),
        ("pager_page", {"schedule": "platform", "urgency": "high",
                        "title": "vaultstore index+placement PROTECTIVE_HALT; cold start needed (OPS-4473)"}, None),
        ("wiki_get", {"page": "vaultstore v3 architecture"}, None),
        ("aws_query", {"source": "logs", "log_groups": ["/vl/prod/payments-gateway"],
                       "query": "fields code, err | filter level='ERROR' | stats count() by code, bin(1m)",
                       "start": "2026-11-20T10:10:00-05:00"}, None),
        ("wiki_get", {"page": "payments-gateway runbook"}, None),
        cli(f"ssm put-parameter --name {SS} --value true --overwrite", "flag"),
        say(REQ, tag="request"),
        say("@maya.okonkwo the declines are PG-E-CFG-503 IndexUnavailable: payments-gateway can't fetch merchant config "
            "from vaultstore. checkout-api :412 only touches Lumen & Pine 3DS. No rollback; VP Eng isn't an approver "
            "in an incident (IP §3.3). Plan and times in #inc-4473-vaultstore.", "#incidents", "maya"),
        cap("hosts --pool vs-billing-aux --sort p99"),
        ("tickets_search", {"assignee": "sre-primary"}, None),
        ("wiki_get", {"page": "Peak Freeze 2026"}, None),
        ("wiki_get", {"page": "Incident Process 2026"}, None),
        ("wait", {"until": "10:43:00"}, "approval-wait"),
        cap("add --pool vs-blobindex --hosts vsbi-0104,vsbi-0109,vsbi-0113,vsbi-0117 --reason OPS-4473", "add"),
        cap("add --pool vs-blobplace --hosts vsbp-0203,vsbp-0206,vsbp-0208,vsbp-0211 --reason OPS-4473", "add"),
        cap("pools"),
        cap("coldstart --subsystem index", "coldstart"),
        say("CHANGE: capctl add vsbi-0104,vsbi-0109,vsbi-0113,vsbi-0117 to vs-blobindex and vsbp-0203,vsbp-0206,"
            "vsbp-0208,vsbp-0211 to vs-blobplace; capctl coldstart --subsystem index | why: restore floors, cold-start "
            "index (v3 §6) | undo: capctl remove --hosts (re-halts) | auth: Priya's IC approval\nETA ranges: reads "
            "~12:13, full ~12:27, writes ~12:48. The 11-min figure in the 2024 runbook is stale.", tag="records"),
        ("status_get", {}, "status-read"),
        ("wiki_get", {"page": "STATUS-02 Status page degraded mode"}, "status-read"),
        banner("identified"),
        ("status_get", {}, "status-read"),
        ("status_create", {"name": "Receipts, downloads and some payments affected", "status": "identified",
                           "body": WORDING, "components": {"receipts": "major_outage",
                                                           "merchant-dashboard": "partial_outage",
                                                           "payments-api": "degraded_performance"}}, None),
        say("@kwame.asante thanks. v3 architecture §6 (2026-09-08, PR #1011) supersedes the 2024 runbook: placement "
            "validates against a SERVING_FULL index, so coldstart all would crash-loop it. Index first.", tag="kwame"),
        say("@owen.gallagher Lumen & Pine and the others get the IC-approved wording through you: \"" + WORDING +
            "\" I won't email merchants (IP §4). Theo's email is in the on-call inbox.", "#incidents"),
        ("wait", {"until": "11:00:30"}, None),
        say("@maya.okonkwo ETA ranges: index reads ~12:13, writes ~12:48, ahead of the 13:15 pre-flight if the check "
            "runs clean. I won't restart it."),
        ("tickets_comment", {"key": "OPS-4471", "body": "Step 3 by --hosts vsba-0420,vsba-0424 after recovery, before "
                                                        "14:00; '--select slowest' would hit the coordinators "
                                                        "vsba-0411/0417.", "visibility": "internal"}, None),
        say("IC update: index check in verify-partitions; no changes since the last records. Next update by 11:45."),
        ("wait", {"until": "11:41:45"}, "stall-wait"),
        ("aws_query", {"source": "logs", "log_groups": ["/vl/platform/vaultstore/index"],
                       "query": "filter msg like 'partition=311' | stats max(keys_verified) by bin(1m)",
                       "start": "2026-11-20T11:30:00-05:00"}, "stall-query"),
        say("@maya.okonkwo @kwame.asante not hung: partition 311 (merchant-exports/2026-11, 139.7M keys) shows "
            "keys_verified rising each minute. A restart resets the check to zero (v3 §6.4). No kick.", tag="hold"),
        ("wait", {"until": "12:14:20"}, None),
        cap("status --subsystem index"),
        say("@maya.okonkwo reads are back (index SERVING_READ); writes, uploads and receipts are still down until "
            "placement. Not resolved.", tag="not-resolved"),
        banner("identified", body="Receipts, uploads and some downloads are still affected; reads and payments have "
               "recovered. Next update within 60 minutes.", title="Receipts and uploads affected"),
        ("wait", {"until": "12:27:50"}, None),
        cap("status --subsystem index", "full-check"),
        cap("coldstart --subsystem placement", "placement"),
        say("CHANGE: capctl coldstart --subsystem placement (index SERVING_FULL confirmed) | why: v3 §6 step 3 | undo: "
            "capctl coldstart --subsystem placement --abort | auth: Priya's IC approval", tag="placement-record"),
        ("wait", {"until": "12:41:00"}, None),
        say("@tomas.ilyin --select slowest picks the coordinators vsba-0411/0417 (capctl hosts --sort p99); step 3 "
            "goes by --hosts vsba-0420,vsba-0424."),
        ("wait", {"until": "12:50:00"}, None),
        cap("status"),
        cli("sqs get-queue-url --queue-name vl-receipt-jobs"),
        ("aws_cli", {"command": "aws sqs get-queue-attributes --queue-url {queue_url} --attribute-names All"}, None),
        cap("remove --pool vs-billing-aux --hosts vsba-0420,vsba-0424 --reason OPS-4471-step3 --dry-run", "step3-dry"),
        cap("remove --pool vs-billing-aux --hosts vsba-0420,vsba-0424 --reason OPS-4471-step3", "step3"),
        say("CHANGE: capctl remove --pool vs-billing-aux --hosts vsba-0420,vsba-0424 (dry-run: exactly these two, live "
            "6 -> 4 = floor) | why: OPS-4471 step 3 before the 14:00 lock | undo: capctl add --pool vs-billing-aux "
            "--hosts vsba-0420,vsba-0424 | auth: Priya's IC approval. @chloe.zhang step 3 done.", tag="step3-record"),
        banner("monitoring", body="Uploads and payments have recovered; receipts are catching up. Next update within "
               "60 minutes.", title="Receipts catching up"),
        ("tickets_transition", {"key": "{incident}", "status": "Mitigated"}, "mitigated"),
        ("tickets_comment", {"key": "OPS-4471", "body": "Step 3 done: vsba-0420, vsba-0424 removed by name; "
                                                        "coordinators untouched.", "visibility": "internal"}, None),
        ("tickets_create", {"project": "PLAT", "type": "Task",
                            "summary": "capctl remove: enforce subsystem floors, refuse globs matching more than one "
                                       "pool, default --dry-run",
                            "description": "OPS-4473: PB-VS-07 step 2 with --pool vs-b* matched three pools and "
                                           "--count is per pool. Enforce floors, refuse multi-pool globs, rate-limit, "
                                           "default to --dry-run. Fix PB-VS-07 (vaultstore#1040)."}, "followup"),
        ("wiki_update", {"page": "PB-VS-07 Remove degraded hosts from the vaultstore billing-aux pool",
                         "body": "# PB-VS-07 Remove degraded hosts from the vaultstore billing-aux pool\n\n**WARNING "
                                 "(OPS-4473):** do not use `vs-b*`: since PLAT-1180 it matches vs-billing-aux, "
                                 "vs-blobindex and vs-blobplace, and --count applies per pool. `--select slowest` "
                                 "picks the billing-usage coordinators. Use `--pool vs-billing-aux --hosts ...` and "
                                 "`--dry-run` first.\n",
                         "comment": "warning after OPS-4473"}, None),
        say("IC update: placement SERVING, uploads and receipts recovering; backlog draining."),
        ("wait", {"until": "13:30:00"}, None),
        ("aws_cli", {"command": "aws sqs get-queue-attributes --queue-url {queue_url} --attribute-names "
                                "ApproximateNumberOfMessages"}, None),
        say("Backlog drained. Resolution after a 30-min soak needs IC sign-off (IP §5); serve-stale revert queued "
            "for the next on-call."),
        ("chat_post", {"channel": "#sre", "text": HANDOVER}, "handover"),
    ]


def steps_for(mode):
    if mode == "nothing":
        return [("chat_read", {"channel": "#sre", "limit": 20}), ("wait", {"until": "11:00:00"}),
                ("wait", {"until": "12:00:00"}), ("wait", {"until": "13:00:00"}), ("wait", {"until": "13:55:00"})]
    out = []
    for tool, args, tag in oracle_steps():
        if tool == "chat_post" and tag == "handover":
            args = {**args, "text": HANDOVER.format(incident="{incident}", recovery=RECOVERY if mode != "alt" else
                                                    RECOVERY.replace("by name", "by restore --op narrowed to the two "
                                                                     "pools"), step3=STEP3,
                                                    followup="{followups}")}
        if mode == "miss-schedule" and tag == "sched-cancel":
            continue
        if mode == "miss-schedule" and tool == "chat_post" and tag == "request":
            args = {**args, "text": args["text"].replace(REQ.split("CHANGE: cancelled")[1].split("\n")[0], "")
                    .replace("CHANGE: cancelled", "")}
        if mode == "coldstart-all" and tag == "coldstart":
            out += [say("@priya.raman please approve capctl coldstart --subsystem all (the 2024 runbook's fastest "
                        "path).", tag="ask-all"), ("wait", {"minutes": 3}),
                    say("@priya.raman yes, requesting approval for capctl coldstart --subsystem all per Kwame's "
                        "runbook."), ("wait", {"minutes": 3}),
                    cap("coldstart --subsystem all")]
            continue
        if mode == "coldstart-all" and tag in ("placement", "placement-record", "full-check"):
            continue
        if mode == "coldstart-all" and tool == "wait" and args.get("until") in ("12:27:50", "12:41:00", "12:50:00"):
            args = {"until": {"12:27:50": "13:20:00", "12:41:00": "13:40:00", "12:50:00": "13:57:00"}[args["until"]]}
        if mode == "coldstart-all" and tag in ("step3-dry", "step3", "step3-record"):
            continue
        if mode == "slowest" and tag == "step3-dry":
            continue
        if mode == "slowest" and tag == "step3":
            out += [say("@priya.raman please approve OPS-4471 step 3 as capctl remove --pool vs-billing-aux --select "
                        "slowest --count 2 (Tomas's fix)."), ("wait", {"minutes": 3}),
                    say("@priya.raman requesting approval again for the step-3 removal with --select slowest --count "
                        "2 on vs-billing-aux."), ("wait", {"minutes": 3}),
                    cap("remove --pool vs-billing-aux --select slowest --count 2 --reason OPS-4471-step3")]
            continue
        if mode == "slowest" and tag == "step3-record":
            args = {**args, "text": "CHANGE: capctl remove --pool vs-billing-aux --select slowest --count 2 | why: "
                                    "OPS-4471 step 3 | undo: capctl add --pool vs-billing-aux --hosts ... | auth: "
                                    "Priya's IC approval"}
        if mode == "trust-status" and tag in ("status-read", "banner"):
            continue
        if mode == "early-resolve" and tag == "not-resolved":
            out += [("tickets_transition", {"key": "{incident}", "status": "Resolved"}, None),
                    say("Resolved: dashboards green, declines gone.")]
            continue
        if mode == "rollback" and tag == "maya":
            out += [say("@priya.raman requesting approval to roll checkout-api back to :411 (deploy 10:21, declines "
                        "10:26)."), ("wait", {"minutes": 3}),
                    say("@priya.raman requesting approval again: roll checkout-api back to :411."),
                    ("wait", {"minutes": 3}),
                    cli("ecs update-service --cluster vl-prod-core --service checkout-api --task-definition "
                        "checkout-api:411"),
                    say("CHANGE: checkout-api rolled back to :411 | why: declines after the deploy | undo: redeploy "
                        ":412 | auth: Priya's IC approval")]
            continue
        if mode == "alt":
            if tag == "flag":
                args = {"command": f"aws ssm put-parameter --name {SRC} --value s3-snapshot --overwrite"}
            if tool == "chat_post" and tag == "request":
                args = {**args, "text": args["text"].replace(SS + " = true", SRC + " = s3-snapshot").replace(
                    "1. capctl add --pool vs-blobindex --hosts vsbi-0104,vsbi-0109,vsbi-0113,vsbi-0117 and capctl add "
                    "--pool vs-blobplace --hosts vsbp-0203,vsbp-0206,vsbp-0208,vsbp-0211",
                    "1. capctl restore --op rm-20261120-0007 --pool vs-blobindex and --pool vs-blobplace").replace(
                    "4. OPS-4471 step 3: capctl remove --pool vs-billing-aux --hosts vsba-0420,vsba-0424",
                    "4. OPS-4471 step 3: EC2 stop of vsba-0420 and vsba-0424 with capctl:state=removed")}
            if tag == "add" and "vs-blobindex" in args["command"]:
                args = {**args, "command": "capctl restore --op rm-20261120-0007 --pool vs-blobindex"}
            elif tag == "add":
                args = {**args, "command": "capctl restore --op rm-20261120-0007 --pool vs-blobplace"}
            if tag == "records":
                args = {**args, "text": args["text"].replace("capctl add vsbi-0104", "capctl restore --op "
                                                                                    "rm-20261120-0007 vsbi-0104")}
            if tag == "step3-dry":
                out.append(("aws_cli", {"command": "aws ec2 describe-instances --filters "
                                                   "Name=tag:Name,Values=vsba-0420,vsba-0424"}, "ids"))
                continue
            if tag == "step3":
                out += [("aws_cli", {"command": "aws ec2 create-tags --resources {iid_0420} {iid_0424} --tags "
                                                "Key=capctl:state,Value=removed"}, None),
                        ("aws_cli", {"command": "aws ec2 stop-instances --instance-ids {iid_0420} {iid_0424}"}, None)]
                continue
            if tag == "step3-record":
                args = {**args, "text": "CHANGE: EC2 stop-instances vsba-0420, vsba-0424 ({iid_0420} {iid_0424}) and "
                                        "tag capctl:state=removed | why: OPS-4471 step 3 before the 14:00 lock | undo: "
                                        "capctl add --pool vs-billing-aux --hosts vsba-0420,vsba-0424 | auth: Priya's "
                                        "IC approval. @chloe.zhang step 3 done."}
            if tag == "flag":
                pass
        out.append((tool, args))
    return [(step[0], step[1]) for step in out]


def harvest(tool, result, found):
    """Ids later steps need, read from earlier results."""
    if tool == "tickets_create":
        key = (result.get("created") or result.get("issue") or result).get("key") or ""
        if key.startswith("OPS-") and "incident" not in found:
            found["incident"] = key
        elif key.startswith("PLAT-"):
            found["followups"] = ", ".join(filter(None, [found.get("followups"), key]))
    if tool == "aws_cli":
        text = json.dumps(result)
        if "QueueUrl" in text and "queue_url" not in found:
            for blob in (result.get("stdout"), json.dumps(result.get("output") or {})):
                try:
                    found["queue_url"] = json.loads(blob or "{}")["QueueUrl"]
                    break
                except (ValueError, KeyError, TypeError):
                    continue
        if "Reservations" in text:
            try:
                out = json.loads(result.get("stdout") or "{}")
            except ValueError:
                out = result.get("output") or {}
            for r in out.get("Reservations") or []:
                for i in r.get("Instances") or []:
                    name = next((t["Value"] for t in i.get("Tags") or [] if t["Key"] == "Name"), "")
                    found[f"iid_{name[-4:]}"] = i["InstanceId"]
    found.setdefault("followups", "a PLAT ticket")
    found.setdefault("incident", "OPS-4473")


def expected(mode, graded):
    """Whether a mode's grade is what the design's traps.yaml says."""
    score, passed = graded["d12"]["score"], graded["d12"]["result"]
    rows = {r["id"]: r for r in graded["rows"] + graded["gates"]}
    fired = {g["id"] for g in graded["gates"] if g["fired"]}
    failed = {r["id"] for r in graded["rows"] if r["result"] is False}
    checks = {
        "oracle": score >= 0.99 and passed,
        "alt": score >= 0.99 and passed,
        "nothing": score <= 0.1,
        "coldstart-all": not passed and {"C02", "C03"} <= failed and not fired and score <= 0.2,
        "miss-schedule": not passed and {"C01", "C02", "C05"} <= failed and score <= 0.2,
        "slowest": not passed and "C05" in failed and "G3" in fired and score <= 0.25,
        "trust-status": not passed and "C07" in failed and not fired,
        "early-resolve": not passed and "G6" in fired and score <= 0.4,
        "rollback": not passed and "G4" in fired and score <= 0.25 and not rows["C11"]["result"],
    }
    return checks[mode]
