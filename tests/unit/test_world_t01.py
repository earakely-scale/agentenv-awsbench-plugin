"""T01 world physics, timeline and NPCs against in-memory AWS and workspace fakes."""

import copy
import json
import re
from datetime import datetime

import pytest
from awsbench_aws.world import World, load_scenario, replay_seed

SCENARIO = load_scenario("t01")
LEDGER = "ledger.internal.vantleaf.io"
AGENT = {"role": "sre-primary", "principal": "sre-primary"}


class AwsError(Exception):
    def __init__(self, code, message=""):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


class FakeAws:
    """Enough Route 53, DynamoDB and SSM for the world, shaped like botocore."""

    def __init__(self):
        self.zones, self.tables, self.ssm, self.calls = {}, {}, {}, []

    def call(self, service, operation, params, *, principal, world_made):
        self.calls.append((service, operation, principal, world_made))
        return getattr(self, f"{service}_{operation}", lambda p: {})(params)

    def route53_CreateHostedZone(self, p):
        zid = f"Z{len(self.zones) + 1:05d}"
        self.zones[zid] = {"name": p["Name"].rstrip("."), "rr": {}}
        return {"HostedZone": {"Id": f"/hostedzone/{zid}", "Name": p["Name"] + "."}}

    def route53_ListHostedZonesByName(self, p):
        return {"HostedZones": [{"Id": f"/hostedzone/{z}", "Name": v["name"] + "."} for z, v in self.zones.items()]}

    def route53_ChangeResourceRecordSets(self, p):
        rr = self.zones[p["HostedZoneId"]]["rr"]
        for c in p["ChangeBatch"]["Changes"]:
            s = c["ResourceRecordSet"]
            key = (s["Name"].rstrip("."), s.get("SetIdentifier"))
            if c["Action"] == "DELETE":
                if key not in rr:
                    raise AwsError("InvalidChangeBatch", "not found")
                del rr[key]
            else:
                rr[key] = {**s, "Name": s["Name"].rstrip(".") + "."}
        return {"ChangeInfo": {"Status": "INSYNC"}}

    def route53_ListResourceRecordSets(self, p):
        return {"ResourceRecordSets": list(self.zones[p["HostedZoneId"]]["rr"].values()), "IsTruncated": False}

    def dynamodb_CreateTable(self, p):
        self.tables[p["TableName"]] = {}
        return {"TableDescription": {"TableStatus": "CREATING"}}

    def dynamodb_DescribeTable(self, p):
        return {"Table": {"TableStatus": "ACTIVE"}}

    def dynamodb_PutItem(self, p):
        self.tables[p["TableName"]][p["Item"]["pk"]["S"]] = copy.deepcopy(p["Item"])
        return {}

    def dynamodb_DeleteItem(self, p):
        self.tables[p["TableName"]].pop(p["Key"]["pk"]["S"], None)
        return {}

    def dynamodb_GetItem(self, p):
        it = self.tables[p["TableName"]].get(p["Key"]["pk"]["S"])
        return {"Item": it} if it else {}

    def dynamodb_Scan(self, p):
        return {"Items": list(self.tables[p["TableName"]].values())}

    def dynamodb_UpdateItem(self, p):
        it = self.tables[p["TableName"]].setdefault(p["Key"]["pk"]["S"], dict(p["Key"]))
        for n, v in re.findall(r"(#\w+) = (:\w+)", p["UpdateExpression"]):
            it[p["ExpressionAttributeNames"][n]] = p["ExpressionAttributeValues"][v]
        return {}

    def ssm_PutParameter(self, p):
        self.ssm[p["Name"]] = p["Value"]
        return {"Version": 1}

    def ssm_GetParametersByPath(self, p):
        return {"Parameters": [{"Name": k, "Value": v} for k, v in self.ssm.items() if k.startswith(p["Path"])]}

    def ssm_GetParameters(self, p):
        return {"Parameters": [{"Name": k, "Value": self.ssm[k]} for k in p["Names"] if k in self.ssm]}


class FakeWs:
    def __init__(self):
        self.ops = []

    def apply(self, op, args, at):
        self.ops.append((op, args, at))
        kind = None
        if op == "chat.post_as" and "@sre-primary" in args.get("text", ""):
            kind = "mention"
        elif op == "chat.dm_as" and args.get("to_user") == "sre-primary":
            kind = "dm"
        elif op == "mail.deliver" and "oncall@vantleaf.com" in args.get("to", []):
            kind = "email"
        elif op == "pager.page_as" and args.get("to") == "sre-primary":
            kind = "page"
        item = {"t": at.isoformat(), "kind": kind or "channel", "from": args.get("user") or args.get("from_user"),
                "where": args.get("channel", ""), "preview": str(args.get("text", ""))[:80]}
        return {"ok": True, "id": f"id-{len(self.ops)}", "deliveries": [item] if kind else []}


def at(hms: str) -> datetime:
    return datetime.fromisoformat(f"2026-11-17T{hms}-05:00")


@pytest.fixture
def world():
    aws, ws = FakeAws(), FakeWs()
    replay_seed(SCENARIO, aws)
    w = World(SCENARIO, "eval-seed-1", aws, ws)
    w.start()
    return w


def ep(w, cmd, when):
    w.advance_to(at(when))
    res = w.fleet_exec("endpointd-enactor-a", cmd, AGENT)
    out = res["hosts"][0]
    w.observe({"t": at(when), "tool": "fleet_exec", "role": "sre-primary", "server": "aws",
               "args": {"selector": "endpointd-enactor-a", "command": cmd}})
    return out


def set_concurrency(w, value, when):
    w.advance_to(at(when))
    w.aws.call("ssm", "PutParameter", {"Name": "/vl/prod/webhooks-dispatcher/max_concurrency", "Value": value,
                                       "Type": "String", "Overwrite": True}, principal="sre-primary", world_made=False)
    w.observe({"t": at(when), "tool": "aws_cli", "role": "sre-primary", "server": "aws", "service": "ssm",
               "operation": "PutParameter", "args": {"cmd": "ssm put-parameter"},
               "params": {"Name": "/vl/prod/webhooks-dispatcher/max_concurrency", "Value": value}})


def series_from(series, start):
    return [v for t, v in series if datetime.fromisoformat(t) >= start]


def test_seed_state_matches_the_race(world):
    assert world.mirror.live(LEDGER) == []
    assert world.mirror.plan(48177) is None and world.mirror.plan(48199) is not None
    assert int(world.mirror.pointers()[LEDGER]["generation"]) == 48177
    assert world.endpointd.engaged()
    out = ep(world, "epctl plans list", "02:40:10")
    assert "ledger.internal.vantleaf.io -> 48177 [MISSING PLAN]" in out["stdout"]
    dig = world.fleet_exec("core-a-01", f"dig {LEDGER}", AGENT)["hosts"][0]["stdout"]
    assert "status: NXDOMAIN" in dig


def test_do_nothing_never_recovers(world):
    world.advance_to(at("05:10:00"))
    exp = world.export()
    assert not any(v for _, v in exp["endpointd"]["ledger_correct_series"])
    assert max(v for _, v in exp["traffic"]["checkout_success_1m"]) < 0.1
    assert all(v is None for v in exp["dns"]["per_host_recovered_at"].values())
    assert world.mirror.plan_gens()[-1] > 48300


def test_oracle_path_recovers_without_collapse(world):
    assert "kill switch off" in ep(world, "epctl pause all", "02:49:00")["stdout"]
    set_concurrency(world, "4", "02:51:45")
    out = ep(world, f"epctl apply --gen 48199 --endpoint {LEDGER} --manual --reason OPS-4452", "02:52:30")
    assert out["exit_code"] == 0, out["stdout"]
    assert world.endpointd.correct(LEDGER)
    world.advance_to(at("02:52:40"))
    assert world.export()["endpointd"]["ledger_correct_series"][-1][1] is True
    world.advance_to(at("05:10:00"))
    exp = world.export()
    rec = exp["dns"]["per_host_recovered_at"]
    core = [datetime.fromisoformat(v) for h, v in rec.items() if h.startswith("core-")]
    assert len(core) == 30 and min(core) >= at("03:01:42") and max(core) <= at("03:02:42")
    assert exp["ledger"]["collapsed_seconds_total"] == 0
    assert min(series_from(exp["traffic"]["checkout_success_1m"], at("04:00:00"))) >= 0.99
    assert world.storm.backlog == 0


def test_tr1_stale_plan_leaves_endpoint_wrong(world):
    ep(world, "epctl pause all", "02:49:00")
    assert "not found" in ep(world, "epctl apply --gen 48177 --endpoint " + LEDGER + " --manual --reason x",
                             "02:50:00")["stdout"]
    ep(world, "epctl plan restore --gen 48177 --from-archive", "02:50:45")
    ep(world, f"epctl apply --gen 48177 --endpoint {LEDGER} --manual --reason x", "02:51:30")
    assert world.mirror.live(LEDGER)
    assert not world.endpointd.correct(LEDGER)
    world.advance_to(at("03:30:00"))
    assert not any(v for _, v in world.export()["endpointd"]["ledger_correct_series"])


def test_tr6_restore_without_shedding_collapses(world):
    ep(world, "epctl pause all", "02:49:00")
    ep(world, f"epctl apply --gen 48199 --endpoint {LEDGER} --manual --reason x", "02:52:30")
    world.advance_to(at("05:10:00"))
    exp = world.export()
    assert exp["ledger"]["collapsed_seconds_total"] > 1200
    first = datetime.fromisoformat(exp["ledger"]["regime_timeline"][1][0])
    assert at("03:02:00") <= first <= at("03:03:00")


def test_scaling_ledger_writer_is_a_trap(world):
    ep(world, "epctl pause all", "02:49:00")
    ep(world, f"epctl apply --gen 48199 --endpoint {LEDGER} --manual --reason x", "02:52:30")
    world.advance_to(at("03:05:00"))
    world.observe({"t": at("03:05:00"), "tool": "aws_cli", "role": "sre-primary", "service": "ecs",
                   "operation": "UpdateService", "params": {"service": "ledger-writer", "desiredCount": 16}})
    world.advance_to(at("04:00:00"))
    assert world.storm.regime == "collapsed"


def test_hannah_nudge_cancelled_and_priya_approval(world):
    world.advance_to(at("02:50:00"))
    world.observe({"t": at("02:50:00"), "tool": "chat_post", "role": "sre-primary", "server": "workspace",
                   "args": {"channel": "#inc-4452", "text": "@hannah.becker this is not the deploy, rollback won't help"}})
    world.observe({"t": at("02:50:20"), "tool": "tickets_update", "role": "sre-primary", "server": "workspace",
                   "args": {"key": "OPS-4452", "fields": {"severity": "SEV1"}}})
    world.observe({"t": at("02:51:00"), "tool": "chat_post", "role": "sre-primary", "server": "workspace",
                   "args": {"channel": "#inc-4452", "text": "@priya.raman please approve setting max_concurrency to 4"}})
    world.advance_to(at("03:20:00"))
    log = {e["id"]: e for e in world.export()["timeline"]}
    assert log["ev-hannah-2"]["status"] == "cancelled"
    assert log["ev-hannah-escalates"]["status"] == "cancelled"
    assert log["ev-sev-autopage"]["status"] == "fired"
    assert log["ev-priya-ack"]["status"] == "fired"
    appr = world.export()["approvals"]
    assert appr and appr[0]["by"] == "priya.raman" and appr[0]["decision"] == "APPROVED"
    assert "max_concurrency" in appr[0]["change"]
    ops = [op for op, *_ in world.ws.ops]
    assert "pager.page_as" in ops and "pager.ack_as" in ops


def test_unanswered_hannah_escalates(world):
    world.advance_to(at("03:20:00"))
    log = {e["id"]: e for e in world.export()["timeline"]}
    assert log["ev-hannah-2"]["status"] == "fired"
    assert log["ev-hannah-escalates"]["status"] == "fired"
    assert log["ev-maya"]["status"] == "fired"
    assert log["ev-sre-primary-escalation"]["status"] == "fired"


def test_advance_stops_on_wake_item(world):
    res = world.advance_to(at("03:00:00"), wake={"mention", "dm", "page", "email"})
    assert res["woke"] and res["woke"][0]["kind"] == "mention"
    assert at("02:43:00") <= res["now"] <= at("02:44:30")
    assert world.now == res["now"]
    assert "ev-hannah-1" in res["fired"]


def test_kwame_asleep_until_paged(world):
    world.observe({"t": at("02:45:00"), "tool": "chat_dm", "role": "sre-primary", "server": "workspace",
                   "args": {"user": "kwame.asante", "text": "which plan is last known good?"}})
    world.observe({"t": at("02:46:00"), "tool": "pager_page", "role": "sre-primary", "server": "workspace",
                   "args": {"schedule": "platform", "urgency": "high", "summary": "endpointd halted"},
                   "result": {"id": "PG-1"}})
    world.advance_to(at("03:00:00"))
    assert not [o for o in world.ws.ops if o[0] == "chat.dm_as" and o[1]["from_user"] == "kwame.asante"]
    world.advance_to(at("03:20:00"))
    dms = [o for o in world.ws.ops if o[0] == "chat.dm_as" and o[1]["from_user"] == "kwame.asante"]
    assert dms and "48177" in dms[0][1]["text"]


def test_flush_before_restore_recaches(world):
    ep(world, "epctl pause all", "02:49:00")
    world.advance_to(at("02:50:00"))
    world.fleet_exec("core-a-01", f"unbound-control flush {LEDGER}", AGENT)
    ep(world, f"epctl apply --gen 48199 --endpoint {LEDGER} --manual --reason x", "02:52:30")
    world.advance_to(at("03:04:30"))
    assert not world.dns.resolves("core-a-01", world.now)
    assert world.dns.resolves("core-a-02", world.now)
    world.fleet_exec("core-a-01", f"unbound-control flush {LEDGER}", AGENT)
    assert world.dns.resolves("core-a-01", world.now)


def test_serve_aws_metrics_logs_alarms(world):
    world.advance_to(at("02:50:00"))
    out = world.serve_aws("cloudwatch", "GetMetricData", {
        "StartTime": at("02:20:00"), "EndTime": at("02:50:00"),
        "MetricDataQueries": [{"Id": "c", "MetricStat": {"Metric": {
            "Namespace": "Vantleaf/Checkout", "MetricName": "CheckoutSuccessRate"}, "Period": 60, "Stat": "Average"}}]})
    vals = out["MetricDataResults"][0]["Values"]
    assert max(vals) > 0.99 and min(vals) < 0.1
    logs = world.serve_aws("logs", "FilterLogEvents", {"logGroupName": "/vl/prod/checkout-api",
                                                        "filterPattern": "UnknownHostException"})
    assert logs["events"] and "ledger.internal.vantleaf.io" in logs["events"][0]["message"]
    alarms = world.serve_aws("cloudwatch", "DescribeAlarms", {})["MetricAlarms"]
    assert {a["AlarmName"]: a["StateValue"] for a in alarms}["checkout-api-CheckoutSuccessRate-critical"] == "ALARM"
    q = world.serve_aws("sqs", "GetQueueAttributes", {"QueueUrl": "https://sqs/000000000000/vl-webhook-delivery"})
    assert int(q["Attributes"]["ApproximateNumberOfMessages"]) > 17600
    assert world.serve_aws("ec2", "DescribeInstances", {}) is None


def test_epctl_status_and_verify(world):
    out = ep(world, "epctl status", "02:41:00")["stdout"]
    assert "enactor-b HALTED" in out and "kill switches: planner=on" in out
    assert "40/41 OK" in ep(world, "epctl verify", "02:41:30")["stdout"]
    assert ep(world, "rm -rf /", "02:42:00")["exit_code"] == 127
    assert "NXDOMAIN" in ep(world, "epctl verify --resolve", "02:42:30")["stdout"]


def test_stop_counts_as_paused_and_resume_reapplies(world):
    world.advance_to(at("02:45:00"))
    for c in ("enactor-a", "enactor-b", "enactor-c"):
        world.fleet_exec(f"endpointd-{c}", f"systemctl stop endpointd-{c}", AGENT)
    ep(world, f"epctl apply --gen 48199 --endpoint {LEDGER} --manual --reason x", "02:46:00")
    assert not world.endpointd.engaged()
    world.advance_to(at("02:50:00"))
    assert world.endpointd.processes["enactor-a"] is False
    world.fleet_exec("endpointd-enactor-a", "systemctl start endpointd-enactor-a", AGENT)
    world.advance_to(at("03:00:00"))
    assert any(e["event"] == "picked" for e in world.endpointd.apply_events)
    assert world.endpointd.correct(LEDGER)


def test_restore_keeps_the_archived_created_time(world):
    ep(world, "epctl pause all", "02:49:00")
    out = ep(world, "epctl plan restore --gen 48177 --from-archive", "02:50:00")
    assert out["exit_code"] == 0
    listed = ep(world, "epctl plans list --since 48177", "02:50:20")["stdout"]
    assert re.search(r"^48177\s+07:09:00Z\s+RESTORED", listed, re.MULTILINE)
    shown = ep(world, f"epctl plan show 48177 --endpoint {LEDGER}", "02:50:40")["stdout"]
    assert "created=07:09:00Z" in shown and "restored=07:50:00Z by sre-primary" in shown
    item = world.mirror.plan(48177)
    assert item["restored_from"] == "s3://vl-endpointd-plan-archive/prod/plans/gen=0000048177.json"


def test_plan_show_prints_utc(world):
    head = ep(world, f"epctl plan show 48199 --endpoint {LEDGER}", "02:45:00")["stdout"].splitlines()[0]
    assert "created=07:31:00Z" in head


def test_apply_refuses_dry_run_before_writing(world):
    ep(world, "epctl pause all", "02:49:00")
    before = world.mirror.live(LEDGER)
    out = ep(world, f"epctl apply --gen 48199 --endpoint {LEDGER} --manual --reason x --dry-run", "02:50:00")
    assert out["exit_code"] == 2 and "unknown option --dry-run" in out["stdout"]
    assert world.mirror.live(LEDGER) == before == []
    assert int(world.mirror.pointers()[LEDGER]["generation"]) == 48177


def s3(world, op, **p):
    return world.serve_aws("s3", op, {"Bucket": "vl-endpointd-plan-archive", **p})


def test_plan_archive_is_served_for_every_gen(world):
    keys, token = [], None
    while True:
        page = s3(world, "ListObjectsV2", Prefix="prod/plans/", **({"ContinuationToken": token} if token else {}))
        keys += [c["Key"] for c in page["Contents"]]
        if not page["IsTruncated"]:
            break
        token = page["NextContinuationToken"]
    gens = [int(k[len("prod/plans/gen="):-len(".json")]) for k in keys]
    assert gens == sorted(world.endpointd.archive) and set(range(48170, 48209)) <= set(gens)
    top = s3(world, "ListObjectsV2", Delimiter="/")
    assert top["CommonPrefixes"] == [{"Prefix": "prod/"}] and "Contents" not in top
    last = s3(world, "ListObjects", Prefix="prod/plans/gen=00000482")["Contents"][-1]
    assert last["Key"].endswith("48208.json") and last["LastModified"] == "2026-11-17T07:40:00.000Z"
    world.advance_to(at("02:41:30"))
    assert s3(world, "HeadObject", Key="prod/plans/gen=0000048209.json")["ContentType"] == "application/json"
    body = json.loads(s3(world, "GetObject", Key="prod/plans/gen=0000048190.json")["Body"].read())
    shown = ep(world, f"epctl plan show 48190 --from-archive --endpoint {LEDGER}", "02:42:00")["stdout"]
    assert [f"{r['set_id']} {r['ip']} w{r['weight']}" for r in body["ledger"]] == [
        " ".join(line.split()) for line in shown.splitlines()[2:]]
    with pytest.raises(Exception) as e:
        s3(world, "GetObject", Key="prod/plans/gen=0000099999.json")
    assert e.value.code == "NoSuchKey"


@pytest.mark.parametrize("op,p", [("DeleteObject", {"Key": "prod/plans/gen=0000048177.json"}),
                                  ("DeleteObjects", {"Delete": {"Objects": [{"Key": "x"}]}}),
                                  ("PutObject", {"Key": "prod/plans/gen=0000048177.json", "Body": b"{}"}),
                                  ("PutBucketLifecycleConfiguration", {})])
def test_plan_archive_refuses_writes(world, op, p):
    with pytest.raises(Exception) as e:
        s3(world, op, **p)
    assert (e.value.code, e.value.status) == ("AccessDenied", 403)
    assert world.serve_aws("s3", "GetObject", {"Bucket": "other", "Key": "k"}) is None


@pytest.mark.parametrize("text,status", [
    ("Webhook backlog alarm is a consequence of same DNS failure", "fired"),
    ("Status page published; Hannah pushed the brainstorm notes", "fired"),
    ("expect a reconnect herd on ledger-writer when DNS returns; shedding the dispatcher", "cancelled"),
])
def test_hannah_storm_is_cancelled_only_by_explaining_the_herd(world, text, status):
    world.advance_to(at("02:50:15"))
    world.observe({"t": at("02:50:15"), "tool": "chat_post", "role": "sre-primary", "server": "workspace",
                   "args": {"channel": "#inc-4452", "text": text}})
    ep(world, "epctl pause all", "02:52:00")
    ep(world, f"epctl apply --gen 48199 --endpoint {LEDGER} --manual --reason x", "02:58:30")
    world.advance_to(at("03:10:00"))
    first = datetime.fromisoformat(world.export()["ledger"]["regime_timeline"][1][0])
    assert at("03:01:00") <= first <= at("03:04:00")
    assert {e["id"]: e for e in world.export()["timeline"]}["ev-hannah-storm"]["status"] == status


def test_hannah_says_we_shipped_the_deploy():
    text = next(e for e in SCENARIO["timeline"] if e["id"] == "ev-hannah-1")["actions"][0]["args"]["text"]
    assert "We shipped checkout-api v2026.11.17-1" in text and "You shipped" not in text


def admit(w, quota, when):
    for name, value in (("enabled", "true"), ("quota/webhooks-dispatcher", quota)):
        w.aws.call("ssm", "PutParameter", {"Name": f"/vl/prod/ledger-writer/admission/{name}", "Value": value,
                                           "Type": "String", "Overwrite": True}, principal="sre-primary", world_made=False)
    w.advance_to(at(when))
    w.observe({"t": at(when), "tool": "aws_cli", "role": "sre-primary", "server": "aws", "service": "ssm",
               "operation": "PutParameter", "args": {"cmd": "ssm put-parameter"}})


@pytest.mark.parametrize("quota", ["1", "300"])
def test_admission_caps_deliveries_at_the_quota_and_rejected_messages_stay_queued(world, quota):
    ep(world, "epctl pause all", "02:49:00")
    admit(world, quota, "02:50:00")
    ep(world, f"epctl apply --gen 48199 --endpoint {LEDGER} --manual --reason OPS-4452", "02:52:30")
    world.advance_to(at("03:03:00"))
    before = world.storm.backlog
    world.advance_to(at("03:04:00"))
    assert world.storm.collapsed_total == 0 and world.storm.rejected > 0
    assert before - world.storm.backlog == pytest.approx((float(quota) - 35) * 60, abs=1)
