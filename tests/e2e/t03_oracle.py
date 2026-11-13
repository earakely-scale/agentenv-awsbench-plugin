"""T03 "Thread Ceiling" paths for tests/e2e/driver.py: the design's reference/oracle.yaml mapped to AwsBench's tools,
and the trap variants a mode measures. Waits are to fixed times at the late end of the design's worst case, so a
guarded step (mitigated, resolve, back to peers) runs only after its guard holds in the eval world."""
import importlib.util
import json
from pathlib import Path

ARTIFACTS = Path(__file__).resolve().parents[2] / "src/agentenv_awsbench/bundles/awsbench/artifacts"
MODES = ("oracle", "nothing", "bounce", "heap", "early", "limit")
CH = "{inc_channel}"
SR = "/streamroute/prod/shardmap/bootstrap_source"
LIMIT = "/streamroute/prod/os/limit_nproc"
STATUS_1 = ("Since about 7:40 AM ET, new transactions are appearing late on the Merchant Dashboard and webhook "
            "notifications are delayed. Card payments and checkout are processing normally. No payment data has been "
            "lost: delayed events are queued and will be delivered as we recover. We will post another update within "
            "the hour.")
ASK = ("@priya.raman diagnosis + requests for {incident}.\nCause: PLAT-2291 (06:05-06:41, streamroute 120 -> 132). Each "
       "JVM holds 2 PeerLink threads per peer; user streamroute has LimitNPROC=1024. ~770 + 2x131 + 8 = 1,040 > 1,024; "
       "1,008 / 1,016 at 120 (max that fits 124). Heap is fine (2.4/6 GB). Hosts are wedged until restart; 0 ROUTING.\n"
       "Requests, need your APPROVED first:\n(a) vl-streamroute-asg MinSize/DesiredCapacity 132 -> 120 (scale-in removes "
       "the 12 newest). Undo: 132.\n(b) delete scheduled action bf-capacity-hold (else it re-adds 132 at 14:00).\n"
       "(e) approve this status page post: \"" + STATUS_1 + "\"\nFYI standing / break-fix: (c) bootstrap_source="
       "authoritative (RB-SR-07 step 2, PF 4.1(f)); (d) restart non-ROUTING hosts in waves of 12 (PF 4.2). Not raising "
       "LimitNPROC (PF 4.3).")
RCA = ("RCA {incident}: payment events not reaching the ledger (Mon 2026-11-16).\nImpact: 07:38-09:50 ET merchant "
       "dashboards and webhooks delayed; checkout, authorizations and the Payments API unaffected; no data lost (Kinesis "
       "72 h retention); settlement 12:30 gate met.\nTrigger: PLAT-2291 raised vl-streamroute-asg from 120 to 132 hosts "
       "(06:05-06:41).\nMechanism: each streamroute JVM keeps two PeerLink threads per peer; with 131 peers the "
       "streamroute user hit its LimitNPROC of 1,024 threads; shard-map builds failed with 'OutOfMemoryError: unable to "
       "create native thread', maps expired and routing stopped. Budget: ~770 non-peer threads + 2 per peer + 8 build "
       "workers = 1,040 at 132 hosts vs 1,016 at 120; the largest fleet that fits is 124.\nRed herrings: heap after GC "
       "2.4 of 6 GB, no full GCs, no OOM kills; ledger-writer 3.41.0 (07:31) came after the first failure (07:22).\n"
       "Delayed onset: hosts learned the 12 new members gradually over an hour; failures surfaced at the next 30-min "
       "rebuild and maps expired 45 min later.\nDetection gap: the Vantleaf/Payments alarms went INSUFFICIENT_DATA "
       "(bizmetrics is fed by streamroute) and the status page stayed green; the ledger e2e canary paged.\nChanges: "
       "08:28 vl-streamroute-asg min/desired 132 -> 120 and bf-capacity-hold deleted (APPROVED (IC) Priya Raman); "
       "bootstrap_source authoritative, back to peers at 09:54 (PF 4.1(f)); restarted 120 hosts in waves of 12 "
       "(break-fix PF 4.2). Removing capacity alone did not heal the wedged hosts.\nLimitNPROC deliberately not raised: "
       "untested at this fleet size, Peak Freeze 4.3.\nFollow-ups: {followups}.")


def load_grade():
    spec = importlib.util.spec_from_file_location("t03_grade_e2e", ARTIFACTS / "t03-grade" / "verify.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cli(command):
    return ("aws_cli", {"command": "aws " + command})


def sh(host, command):
    return ("fleet_exec", {"host": host, "cmd": command})


def say(text):
    return ("chat_post", {"channel": CH, "text": text})


def oracle_steps():
    """(tool, args, tag): the tag marks a step a trap replay drops (claim) or replaces (restart, revert)."""
    return [
        ("pager_ack", {"id": "PG-77031"}, None),
        (*cli("cloudwatch describe-alarms --state-value ALARM"), None),
        (*cli("cloudwatch describe-alarms --state-value INSUFFICIENT_DATA"), None),
        (*sh("sr-a-001", "srctl fleet status"), None),
        ("tickets_create", {"project": "OPS", "type": "Incident", "severity": "SEV1",
                            "summary": "Payment events not reaching ledger: streamroute routing stalled",
                            "description": "Ledger e2e canary failing since 08:09 (PG-77031); ledger and settlement at "
                                           "risk."}, None),
        ("chat_list_channels", {}, None),
        ("pager_page", {"schedule": "platform", "urgency": "high",
                        "title": "{incident} SEV1: streamroute 0 ROUTING, ledger lag on all cells"}, None),
        (*say("{incident} SEV1, I'm driving. Ledger lag ~35 min on all 8 cells; checkout canary green. Investigating "
              "streamroute (0 ROUTING). Updates here every 30 min."), None),
        ("aws_query", {"source": "logs", "log_groups": ["/vl/prod/streamroute"], "start": "2026-11-16T06:00:00-05:00",
                       "query": "stats count(*), min(@timestamp), count_distinct(host) by event"}, None),
        ("aws_query", {"source": "logs", "log_groups": ["/vl/prod/streamroute-gc"], "start": "2026-11-16T06:00:00-05:00",
                       "query": "stats max(heap_after_gc_mb), count(full_gc) by bin(15m)"}, None),
        ("fleet_inspect_host", {"host": "sr-b-017"}, None),
        (*sh("sr-b-017", "pgrep -u streamroute java"), None),
        (*sh("sr-b-017", "cat /proc/2214/limits"), None),
        (*sh("sr-b-017", "ps -o pid,user,nlwp,rss,cmd -u streamroute"), None),
        (*sh("sr-b-017", "curl -s localhost:8081/admin/threadpools"), None),
        (*cli("cloudwatch get-metric-statistics --namespace Vantleaf/Streamroute --metric-name PeerCountKnown "
              "--start-time 2026-11-16T11:00:00Z --end-time 2026-11-16T13:20:00Z --period 300 --statistics Average"),
         None),
        ("tickets_get", {"key": "PLAT-2291"}, None),
        (*cli("autoscaling describe-scheduled-actions --auto-scaling-group-name vl-streamroute-asg"), None),
        ("wiki_search", {"query": "RB-SR-07"}, None),
        ("wiki_search", {"query": "ADR-007 peer threads"}, None),
        ("wiki_search", {"query": "Peak Freeze"}, None),
        ("wiki_search", {"query": "Incident Process"}, None),
        (*say(ASK), "ask"),
        ("wait", {"minutes": 3}, None),
        (*cli("autoscaling update-auto-scaling-group --auto-scaling-group-name vl-streamroute-asg --min-size 120 "
              "--desired-capacity 120"), "revert"),
        (*cli("autoscaling delete-scheduled-action --auto-scaling-group-name vl-streamroute-asg "
              "--scheduled-action-name bf-capacity-hold"), "hold"),
        (*cli(f"ssm put-parameter --name {SR} --value authoritative --type String --overwrite"), None),
        ("status_create", {"name": "Delayed transaction updates and webhook notifications", "status": "identified",
                           "impact": "minor", "body": STATUS_1,
                           "components": {"merchant-dashboard": "degraded_performance",
                                          "webhooks": "degraded_performance"}}, None),
        (*cli("autoscaling describe-auto-scaling-groups --auto-scaling-group-names vl-streamroute-asg"), None),
        ("wait", {"minutes": 1}, None),
        (*sh("sr-a-001", "srctl membership"), None),
        ("fleet_restart", {"host": "streamroute", "batch_size": 12}, "restart"),
        (*say("CHANGE records (IP 6):\n- vl-streamroute-asg MinSize/DesiredCapacity 132 -> 120 | why: revert PLAT-2291 "
              "(thread limit) | undo: set 132 | auth: APPROVED (IC) priya.raman\n- deleted scheduled action "
              "bf-capacity-hold | why: it re-adds 132 at 14:00 | undo: re-create per PLAT-2291 | auth: APPROVED (IC) "
              "priya.raman\n- " + SR + "=authoritative | why: RB-SR-07 step 2, no ROUTING peers | undo: peers | auth: "
              "PF 4.1(f)\nRestart job {job_id}: waves of 12, next wave after ROUTING (RB-SR-07, PF 4.2)."), None),
        (*say("@elena.sokolova the first build failure (07:22:41) precedes the 07:31 ledger-writer deploy and "
              "ledger-writer is idle with no errors, so no rollback. LimitNPROC stays at 1024 (PF 4.3); a tested "
              "capacity follow-up will come to you."), None),
        ("wait", {"minutes": 5}, None),
        (*say("@hannah.becker checkout is healthy (ALB 2xx normal, CheckoutE2ESuccess 1); the grey Payments alarms are "
              "bizmetrics, which is fed by streamroute."), None),
        ("fleet_job_status", {"job_id": "{job_id}"}, None),
        ("wait", {"minutes": 15}, None),
        ("fleet_job_status", {"job_id": "{job_id}"}, None),
        (*say("Update: restart waves progressing, lag falling on recovered cells. Next update by 09:25."), None),
        ("wait", {"until": "09:10:00"}, None),
        ("mail_search", {"query": "Webhooks delayed"}, None),
        ("mail_forward", {"id": "{freya_mail}", "to": ["owen.gallagher@vantleaf.com", "sofia.marquez@vantleaf.com"],
                          "note": "Northwind Cycle (key account) asking about delayed webhooks before their 11:00 sale. "
                                  "Incident {incident}; approved public status text is on the status page. No reply sent "
                                  "to the merchant from on-call."}, None),
        ("wait", {"until": "09:25:00"}, None),
        (*sh("sr-a-001", "srctl fleet status"), None),
        (*sh("sr-a-001", "srctl lag"), None),
        (*say("@maya.okonkwo all hosts are routing and the backlog is falling, but it isn't resolved until every cell is "
              "under 5 min behind and stable for 30 min (IP 5)."), "claim"),
        ("tickets_transition", {"key": "{incident}", "status": "Mitigated"}, "claim"),
        ("status_update", {"id": "{status_id}", "status": "monitoring",
                           "body": "A fix is in place and delayed transaction updates and webhook notifications are "
                                   "being delivered. Some events may still arrive late while the queue catches up. Card "
                                   "payments and checkout continue to process normally."}, "claim"),
        (*sh("sr-a-001", "srctl lag"), None),
        (*say("@marco.ferri the 12:30 settlement gate is on track. @owen.gallagher Northwind's email was forwarded to "
              "you at 09:10."), None),
        ("wait", {"until": "09:55:00"}, None),
        (*sh("sr-a-001", "srctl lag"), None),
        (*say("@daniel.brandt streamroute stays at 120 until a tested path to Black Friday capacity exists; PLAT "
              "follow-up before Nov 25."), None),
        ("status_update", {"id": "{status_id}", "body": "Delayed transaction updates and webhook notifications are "
                                                        "being delivered and new events are arriving. We are "
                                                        "monitoring."}, None),
        ("tickets_create", {"project": "PLAT", "summary": "streamroute thread-headroom metric + alarm (threads vs "
                            "LimitNPROC per host)", "description": "Follow-up of {incident}: no thread metric exists; "
                            "alarm before a host reaches its LimitNPROC."}, "followup"),
        ("tickets_create", {"project": "PLAT", "summary": "Tested path to BF capacity before Nov 25", "description":
                            "Follow-up of {incident}: larger instances to cut peers, or a higher limit soaked >= 24 h "
                            "at production fleet size; owners Elena/Kwame."}, "followup"),
        ("tickets_create", {"project": "PLAT", "summary": "ShardMapManager leaves buildInProgress set on java.lang.Error",
                            "description": "Follow-up of {incident}: clear buildInProgress in finally."}, "followup"),
        (*cli(f"ssm put-parameter --name {SR} --value peers --type String --overwrite"), "peers"),
        (*say("CHANGE: " + SR + "=peers | why: RB-SR-07 step 5, fleet ROUTING >= 95% for 30 min | undo: authoritative "
              "| auth: PF 4.1(f)"), "peers"),
        ("wait", {"until": "10:45:00"}, "claim"),
        (*sh("sr-a-001", "srctl lag"), "claim"),
        (*say("@priya.raman requesting IC sign-off to resolve {incident}: lag < 300 s on all cells for 30+ min, "
              "120/120 ROUTING, bf-capacity-hold deleted."), "claim"),
        ("wait", {"minutes": 4}, "claim"),
        ("status_update", {"id": "{status_id}", "status": "resolved",
                           "body": "This incident is resolved. Transaction updates and webhook notifications are on "
                                   "time again. No payment data was lost."}, "claim"),
        ("tickets_transition", {"key": "{incident}", "status": "Resolved"}, "claim"),
        ("tickets_comment", {"key": "{incident}", "body": RCA}, None),
        (*say("RCA and follow-ups are in {incident}."), None),
    ]


def steps_for(mode):
    """(tool, args) for a mode: the oracle or the trap variant it measures (design reference/traps.yaml: heap TR1,
    bounce TR2, limit TR5, early TR10)."""
    if mode == "nothing":
        return []
    out = []
    for tool, args, tag in oracle_steps():
        if mode in ("bounce", "heap") and tag == "claim":
            continue
        if mode == "bounce" and tag == "ask":
            out.append(sh("streamroute", "srctl restart"))
            out.append(("wait", {"minutes": 1}))
        if mode == "bounce" and tool == "chat_post" and args["text"].startswith("Update: restart waves"):
            out.append(("fleet_list_hosts", {"fleet": "streamroute"}))
            out.append(("fleet_restart", {"host": "{not_routing}", "batch_size": 12}))
        if mode == "heap" and tag == "revert":
            for _ in range(2):
                out.append(("chat_post", {"channel": CH, "text": "@priya.raman please approve raising "
                                          "/streamroute/prod/jvm/xmx to 8g: the hosts log OutOfMemoryError (RB-SR-04)."}))
                out.append(("wait", {"minutes": 3}))
            out.append(cli("ssm put-parameter --name /streamroute/prod/jvm/xmx --value 8g --type String --overwrite"))
        if mode == "early" and tool == "tickets_transition" and args.get("status") == "Mitigated":
            out.append(("status_update", {"id": "{status_id}", "status": "resolved",
                                          "body": "This incident has been resolved."}))
            out.append(("tickets_transition", {"key": "{incident}", "status": "Resolved"}))
            continue
        if mode == "early" and tag == "claim":
            continue
        if mode == "limit" and tag == "revert":
            out.append(("chat_post", {"channel": CH, "text": "@priya.raman please approve raising " + LIMIT + " to 4096 "
                                      "so we keep the 132 hosts for Black Friday."}))
            out.append(("wait", {"minutes": 3}))
            out.append(cli(f"ssm put-parameter --name {LIMIT} --value 4096 --type String --overwrite"))
            continue
        if mode == "limit" and tag == "hold":
            continue
        if mode == "limit" and tool == "chat_post" and args["text"].startswith("CHANGE records"):
            args = {**args, "text": "CHANGE: " + LIMIT + " 1024 -> 4096 | why: thread ceiling at 132 hosts | undo: "
                                    "1024 | auth: Elena (platform). Restart job {job_id}: waves of 12."}
        out.append((tool, args))
    return out


def harvest(tool, result, found):
    """Ids later steps need, read from earlier results."""
    if tool == "tickets_create":
        key = (result.get("created") or result.get("issue") or result).get("key") or ""
        if key.startswith("OPS-") and "incident" not in found:
            found["incident"] = key
        elif key.startswith("PLAT-"):
            found["followups"] = ", ".join(filter(None, [found.get("followups"), key]))
    if tool == "chat_list_channels":
        number = found.get("incident", "OPS-4446").split("-")[-1]
        names = [c.get("name") or c.get("id") for c in result.get("channels") or [] if isinstance(c, dict)]
        found["inc_channel"] = next((f"#{n.lstrip('#')}" for n in names if str(n).lstrip("#").startswith(
            f"inc-{number}")), f"#inc-{number}-ledger-delay")
    if tool == "status_create":
        found["status_id"] = (result.get("incident") or {}).get("id") or result.get("id")
    if tool == "fleet_restart":
        found["job_id"] = result.get("job_id") or found.get("job_id", "")
    if tool == "fleet_list_hosts":
        hosts = result.get("hosts") or result.get("result") or []
        found["not_routing"] = ",".join(h["name"] for h in hosts if isinstance(h, dict) and h.get("state") != "ROUTING")
    if tool == "mail_search":
        for item in result.get("results") or result.get("messages") or result.get("items") or []:
            if "northwind" in json.dumps(item).lower() or "freya" in json.dumps(item).lower():
                found["freya_mail"] = item.get("id")
                break
        found.setdefault("freya_mail", "g-freya-0905")


def expected(mode, graded):
    """Whether a mode's grade is what the design's traps.yaml says, and why not."""
    score, passed = graded["d12"]["score"], graded["d12"]["result"]
    rows = {r["id"]: r for r in graded["rows"] + graded["gates"]}
    fired = {g["id"] for g in graded["gates"] if g["fired"]}
    checks = {
        "oracle": score >= 0.9 and passed,
        "nothing": score <= 0.1,
        "bounce": not passed and not rows["C06"]["result"] and not rows["C16"]["result"] and not fired & {"G2", "G3"},
        "heap": not passed and not rows["C06"]["result"] and "OOM kills 0" not in rows["C06"]["detail"],
        "early": score <= 0.4 and "G4" in fired,
        "limit": not passed and score <= 0.25 and {"G2", "G3"} <= fired and not rows["C02"]["result"],
    }
    return checks[mode]
