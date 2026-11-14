import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from awsbench_aws.account import ACCOUNT, Shown
from awsbench_aws.bridge import operation_name
from awsbench_aws.cloudtrail import event
from awsbench_aws.meter import parse_time
from awsbench_aws.server import (
    EXPORT_MAX_CHARS,
    NO_SCENARIO,
    AwsServer,
    count_hosts,
    fleet_outcome,
    lookup_attributes_of,
    profiles_of,
)
from awsbench_aws.world import World, replay_seed
from awsbench_aws.world.fleet import SHELL_OPS
from fastmcp import Client
from test_workspace_server import ROLE, make
from test_world_t01 import SCENARIO, FakeAws, FakeWs

SRE, HARNESS = SimpleNamespace(role="sre-primary"), SimpleNamespace(role="harness")


async def test_charged_calls_without_a_scenario_are_refused_uncharged():
    server = AwsServer()

    async def work(call):
        raise AssertionError("ran without a scenario")

    out = await server._metered(SRE, "aws_cli", {"command": "aws route53 list-hosted-zones"}, work)
    assert NO_SCENARIO in out and server.meter.calls_used == 0
    assert NO_SCENARIO in await server.wait(SRE, minutes=5)
    assert server.meter.calls_used == 0


async def test_uncharged_roles_still_run_without_a_scenario():
    server = AwsServer()

    async def work(call):
        return {"ran": True}

    server.ready.set()
    assert '"ran":true' in await server._metered(HARNESS, "fleet_list_hosts", {}, work)


def test_wait_minutes_are_clamped_before_the_date_math():
    server = AwsServer()
    now = server.meter.now
    assert (server._wait_target(1e308, None) - now).total_seconds() == server.meter.cfg.wait_max_s


@pytest.mark.parametrize("value", [
    {"EventName": "PutParameter"},
    {"AttributeKey": "EventName", "AttributeValue": "PutParameter"},
    [{"EventName": "PutParameter"}],
    [{"AttributeKey": "EventName", "AttributeValue": "PutParameter"}],
])
def test_cloudtrail_lookup_attributes_take_every_shape(value):
    assert lookup_attributes_of(value) == [{"AttributeKey": "EventName", "AttributeValue": "PutParameter"}]


def test_cloudtrail_lookup_attributes_refuse_more_than_one_and_non_objects():
    assert lookup_attributes_of(None) == []
    with pytest.raises(ValueError, match="one attribute"):
        lookup_attributes_of({"EventName": "PutParameter", "Username": "kwame.asante"})
    with pytest.raises(TypeError, match="an object"):
        lookup_attributes_of(["EventName=PutParameter"])


def test_fleet_exec_says_which_syntax_it_refuses():
    text = AwsServer.fleet_exec._aee_tool.description
    ops = [";", "|", "&&", "<", ">", "`", "$("]
    assert f"One command per call: shell operators ({' '.join(ops)} )) are refused, uncharged" in text
    assert all(SHELL_OPS.search(f"epctl status {op} epctl verify") for op in ops)


def test_fleet_outcome():
    ok = {"hosts": [{"host": "a", "exit_code": 0}, {"host": "b", "exit_code": 0}]}
    assert fleet_outcome(ok) == (0, None)
    bad = {"hosts": [{"host": "a", "exit_code": 0}, {"host": "b", "exit_code": 127}, {"host": "c", "exit_code": 1}]}
    assert fleet_outcome(bad) == (127, "exit code 127 on b, c")
    assert fleet_outcome({"hosts": [], "error": "no hosts match 'x'"}) == (1, "no hosts match 'x'")
    assert fleet_outcome({"job_id": "job-0001", "hosts": 3, "batches": 3}) == (0, None)


async def test_fleet_rows_carry_the_exit_code():
    server = AwsServer()
    server.ready.set()
    server.scenario = {}

    async def work(call):
        return {"hosts": [{"host": "ep-enactor-a", "stdout": "error: plan gen=48177 not found", "exit_code": 1}]}

    out = await server._metered(SRE, "fleet_exec", {"selector": "ep-enactor-a", "command": "epctl apply --gen 48177"},
                                work, hosts=1)
    assert '"exit_code":1' in out
    (row,) = server.actionlog.export()
    assert row["tool"] == "fleet_exec" and row["exit_code"] == 1 and row["error"] == "exit code 1 on ep-enactor-a"


class PastClock:
    def __init__(self, now: datetime):
        self.now = now

    async def time(self):
        return self.now

    async def advance(self, t):
        raise AssertionError("a refused configure moved the clock")


async def test_configure_refuses_when_the_gateway_clock_is_past_the_start():
    server = AwsServer()
    server.ready.set()
    server.clock = PastClock(parse_time("2026-11-17T04:06:00-05:00"))
    with pytest.raises(ValueError, match="gateway clock .* is past the scenario start.*deploy a fresh env"):
        await server.configure(scenario="t01", seed="s")
    assert server.scenario is None and server.world is None


@pytest.fixture
def world():
    aws = FakeAws()
    replay_seed(SCENARIO, aws)
    w = World(SCENARIO, "eval-seed-1", aws, FakeWs())
    w.start()
    return w


@pytest.mark.parametrize("selector", ["vl-core,endpointd-enactor-a,endpointd-enactor-b", "use1-az6",
                                      "vl-core:use1-az6", "vl-core,nope-*", "core-a-*", "*"])
def test_count_hosts_is_the_worlds_selection(world, selector):
    assert count_hosts(world, selector) == len(world.fleet.select(selector))


def test_fleet_writes_carry_the_roles_aws_principal(world):
    server = AwsServer()
    server.bridge.profiles = profiles_of(SCENARIO)
    caller = server._fleet_caller(SRE, {"call_no": 7, "t": "2026-11-17T02:49:00-05:00"})
    assert caller["role"] == "sre-primary" and caller["principal"] == "assumed-role/SRE-OnCall/sre-primary"
    world.aws.calls.clear()
    world.fleet_exec("endpointd-enactor-a", "epctl pause all", caller)
    puts = [c for c in world.aws.calls if c[1] == "PutParameter"]
    assert puts and all(c[2] == caller["principal"] and not c[3] for c in puts)
    ev = event(world.now, "ssm", "PutParameter", {"Name": "x"}, caller["principal"])
    assert ":assumed-role/SRE-OnCall/sre-primary" in ev["CloudTrailEvent"] and ev["Username"] == "sre-primary"


def lease_scan(n=240, page=130):
    items = [{"leaseKey": {"S": f"shardId-{k:012d}"}, "leaseOwner": {"S": f"sr-a-{k % 40 + 1:03d}"},
              "leaseCounter": {"N": "18560"}, "checkpoint": {"S": "4965440113574308133358427126538051415931602213030"
                                                                    f"{k:07d}"}, "checkpointSubSequenceNumber": {"N": "0"},
              "ownerSwitchesSinceCheckpoint": {"N": "0"}, "parentShardId": {"S": "-"}} for k in range(n)]
    calls = []

    def call_api(service, op, params, **kw):
        calls.append(params)
        start = int((params.get("ExclusiveStartKey") or {}).get("i", 0))
        out = {"Items": items[start:start + page]}
        if start + page < n:
            out["LastEvaluatedKey"] = {"i": start + page}
        return out
    return call_api, calls


async def test_table_export_pages_under_a_size_cap_with_the_banner_first():
    server = AwsServer()
    server.ready.set()
    server.bridge.call_api, _ = lease_scan()
    seen, offset, first = [], 0, None
    while True:
        text = await server.aws_table_export("streamroute-leases", HARNESS, limit=300, offset=offset)
        first = first or text
        assert text.startswith('{"_clock"')
        out = json.loads(text)
        assert len(json.dumps(out["items"], separators=(",", ":"))) <= EXPORT_MAX_CHARS
        seen += [i["leaseKey"] for i in out["items"]]
        if not out["truncated"]:
            assert "next_offset" not in out
            break
        assert out["next_offset"] == offset + out["count"]
        offset = out["next_offset"]
    assert json.loads(first)["count"] < 240 and sorted(seen) == sorted(set(seen)) and len(seen) == 240
    out = json.loads(await server.aws_table_export("streamroute-leases", HARNESS, attributes="leaseKey,leaseOwner"))
    assert out["count"] == 240 and not out["truncated"] and set(out["items"][0]) == {"leaseKey", "leaseOwner"}
    out = json.loads(await server.aws_table_export("streamroute-leases", HARNESS, limit=10))
    assert out["count"] == 10 and out["truncated"] and out["next_offset"] == 10


async def test_an_unknown_fleet_or_zone_is_a_charged_error():
    server = AwsServer()
    server.ready.set()
    server.scenario = {}

    class W:
        def fleet_list_hosts(self, fleet, az):
            raise ValueError("unknown fleet 'ledger-writer'; fleets: cellmap-admin, streamroute")
    server.world = W()
    out = json.loads(await server.fleet_list_hosts(SRE, fleet="ledger-writer"))
    assert out["hosts"] == [] and "fleets: cellmap-admin, streamroute" in out["error"]
    assert server.meter.calls_used == 1


def test_the_scenario_account_is_shown_and_mapped_back():
    shown = Shown("444444444444")
    text = json.dumps({"Arn": f"arn:aws:iam::{ACCOUNT}:role/x", "Account": ACCOUNT, "shard": f"shardId-{ACCOUNT}",
                       "image": f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/x", "seq": "1" + ACCOUNT + "2",
                       "value": "1." + ACCOUNT})
    out = json.loads(shown.out(text))
    assert out["Arn"] == "arn:aws:iam::444444444444:role/x" and out["Account"] == "444444444444"
    assert out["shard"] == f"shardId-{ACCOUNT}" and out["image"].startswith("444444444444.dkr")
    assert out["seq"] == "1" + ACCOUNT + "2" and out["value"] == "1." + ACCOUNT
    assert shown.inside({"Arn": ["arn:aws:sns:us-east-1:444444444444:t"]}) == {"Arn": [f"arn:aws:sns:us-east-1:{ACCOUNT}:t"]}
    assert Shown().out(text) == text and Shown(None).inside("444444444444") == "444444444444"


async def test_aws_api_sees_the_emulator_account_and_answers_the_scenario_one():
    server = AwsServer()
    server.ready.set()
    server.shown = Shown("444444444444")
    got = []

    def call_api(service, op, params, **kw):
        got.append(params)
        return {"Account": ACCOUNT, "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/SRE-OnCall/sre"}
    server.bridge.call_api = call_api
    out = json.loads(await server.aws_api("sns", "GetTopicAttributes", HARNESS,
                                          params={"TopicArn": "arn:aws:sns:us-east-1:444444444444:vl-pager-sre"}))
    assert got == [{"TopicArn": f"arn:aws:sns:us-east-1:{ACCOUNT}:vl-pager-sre"}]
    assert out["Account"] == "444444444444" and ACCOUNT not in json.dumps(out)


async def test_workspace_results_carry_the_banner_first():
    _, app = make()
    async with Client(app) as c:
        r = await c.call_tool("tickets_get", {"key": "OPS-4452"}, meta=ROLE)
    assert r.content[0].text.startswith('{"_clock"')


def test_aws_query_description_names_nothing_of_a_scenario():
    from awsbench_aws.server import AwsServer, load_scenario_file
    text = AwsServer.aws_query._aee_tool.description
    tokens = {"NXDOMAIN", "ChangeResourceRecordSets"}
    for name in ("t01", "t03"):
        s = load_scenario_file(name, {})
        tokens |= set(s.get("npcs") or {}) | {r["operation"] for r in s.get("trail_history") or []}
    assert not [t for t in tokens if t in text]


def t01_trail_api():
    from awsbench_aws.actionlog import ActionLog
    from awsbench_aws.cloudtrail import Trail, seed_trail
    from awsbench_aws.server import load_scenario_file, seed_rows
    t01 = load_scenario_file("t01", {})
    trail = Trail(ActionLog(), seed_trail(t01, seed_rows(t01), {"internal.vantleaf.io": "ZVKATFGSZ8LHTCP"}))
    return lambda service, op, params, **kw: trail.lookup(params)


ROUTE53 = {"source": "cloudtrail", "lookup_attributes": {"EventSource": "route53.amazonaws.com"},
           "start": "2026-11-16T12:00:00Z", "end": "2026-11-17T07:50:30Z", "limit": 30}


async def test_cloudtrail_rows_are_compact_under_the_cap():
    """R11-opus-2 CALL 18: this query, with full events, was 62,545 characters and the client spilled it to a file."""
    server = AwsServer()
    server.ready.set()
    server.bridge.call_api = t01_trail_api()
    text = await server.aws_query(HARNESS, **ROUTE53)
    out = json.loads(text)
    assert len(out["events"]) == 30 and out["truncated"] is True and out["next_offset"] == 30
    assert len(json.dumps(out["events"], ensure_ascii=False, separators=(",", ":"))) <= 40_000
    row = out["events"][0]
    assert "CloudTrailEvent" not in row and row["Role"] == "vl-endpointd-enactor-role"
    assert row["requestParameters"]["changeBatch"]["Changes"]


async def test_full_cloudtrail_events_page_by_end_and_offset():
    server = AwsServer()
    server.ready.set()
    server.bridge.call_api = t01_trail_api()

    async def pages(**kw):
        seen, offset, n = [], 0, 0
        while True:
            out = json.loads(await server.aws_query(HARNESS, **ROUTE53, **kw, offset=offset))
            n += 1
            assert len(json.dumps(out["events"], ensure_ascii=False, separators=(",", ":"))) <= 40_000
            seen += [e["EventId"] for e in out["events"]]
            if not out.get("truncated"):
                return seen, n
            assert out["end"] == "2026-11-17T07:50:30Z" and out["next_offset"] == offset + len(out["events"])
            offset = out["next_offset"]
    full, n_full = await pages(full=True)
    compact, n_compact = await pages()
    assert n_full > 1 and n_compact > 1 and full == compact and len(set(full)) == len(full)


async def test_cloudtrail_says_when_limit_stopped_it():
    """R13-fable CALL 37: limit 10 returned exactly 10 events with nothing to say more existed."""
    server = AwsServer()
    server.ready.set()
    server.bridge.call_api = t01_trail_api()
    first = json.loads(await server.aws_query(HARNESS, **{**ROUTE53, "limit": 10}))
    assert len(first["events"]) == 10 and first["truncated"] is True and first["next_offset"] == 10
    second = json.loads(await server.aws_query(HARNESS, **{**ROUTE53, "limit": 10}, offset=10))
    ids = {e["EventId"] for e in first["events"]}
    assert len(second["events"]) == 10 and not ids & {e["EventId"] for e in second["events"]}
    whole = json.loads(await server.aws_query(HARNESS, **{**ROUTE53, "limit": 1000}))
    assert [e["EventId"] for e in whole["events"]][:20] == [
        e["EventId"] for e in first["events"] + second["events"]]


def test_t01_restart_options_are_refused_before_the_meter(world):
    """R11-opus-3 saw T03's wait_for and pause_seconds on T01, whose world refused them only after charging a write."""
    from awsbench_aws.server import unrunnable
    from test_t03_world import world as t03_world
    out = unrunnable("fleet_restart", {"selector": "vl-core", "batch_size": 5, "pause_seconds": 60}, world)
    assert out["charged"] is False and "about 90 s apart" in out["error"]
    assert unrunnable("fleet_restart", {"selector": "vl-core", "batch_size": 5}, world) is None
    assert unrunnable("fleet_restart", {"selector": "streamroute", "batch_size": 12, "wait_for": "ready",
                                        "pause_seconds": 60}, t03_world()) is None


def api_server():
    server = AwsServer()
    server.ready.set()
    server.scenario = {}
    server.bridge.profiles = profiles_of(SCENARIO)
    server.bridge.call_api = lambda service, operation, params, **kw: {}
    return server


@pytest.mark.parametrize("operation,cls", [("describe_alarms", "point_read"), ("DescribeAlarms", "point_read"),
                                           ("get_metric_statistics", "point_read"), ("start_query", "broad"),
                                           ("put_parameter", "write")])
async def test_aws_api_meters_snake_case_by_the_api_name(operation, cls):
    """R12-haiku-1: 26 reads given in snake_case were charged as 45 s writes."""
    server, seen = api_server(), []
    server.bridge.call_api = lambda service, operation, params, **kw: seen.append(operation) or {}
    service = {"start_query": "logs", "put_parameter": "ssm"}.get(operation, "cloudwatch")
    await server.aws_api(service, operation, SRE, params={})
    assert server.meter.rows[-1]["cost_class"] == cls and seen == [operation_name(service, operation)]
    assert seen[0][0].isupper()


def test_operation_names():
    assert operation_name("cloudwatch", "describe_alarms") == "DescribeAlarms"
    assert operation_name("route53", "test_dns_answer") == "TestDNSAnswer"
    assert operation_name("logs", "start_query") == "StartQuery"
    assert operation_name("ssm", "put_parameter") == "PutParameter"
    assert operation_name("nosuch", "x") == "x"


async def test_an_unknown_profile_fails_in_the_cli_uncharged():
    """R12-haiku-1 CALLs 22-23: `--profile vl-prod` answered a fake AccessDenied and was charged 20 s."""
    server = api_server()
    server.bridge.run_cli = lambda command, **kw: {"exit_code": 0, "stdout": "{}", "stderr": ""}
    out = json.loads(await server.aws_cli("aws logs describe-log-groups --profile vl-prod", SRE))
    assert out["exit_code"] == 255 and out["charged"] is False and "could not be found" in out["stderr"]
    assert server.meter.calls_used == 0 and not server.meter.rows
    out = json.loads(await server.aws_api("cloudwatch", "DescribeAlarms", SRE, profile="vl-prod"))
    assert out["error"]["Code"] == "ProfileNotFound" and out["charged"] is False and server.meter.calls_used == 0
    await server.aws_cli("aws logs describe-log-groups --profile vl-prod/SRE-OnCall", SRE)
    assert server.meter.calls_used == 1


async def test_an_aws_row_records_when_the_call_was_issued():
    """R13-fable-1 seq 70: an ssm PutParameter row had t_issue = t, the execution time."""
    server = api_server()
    seen = {}

    async def work(call):
        seen.update(call)
        return {}
    await server._metered(SRE, "aws_cli", {"command": "aws ssm put-parameter --name /vl/prod/x --value 1 --overwrite"},
                          work)
    row = server.meter.rows[-1]
    assert seen["t_issue"] == parse_time(row["t_issue"]) and seen["t"] == parse_time(row["t_exec"])
    assert (seen["t"] - seen["t_issue"]).total_seconds() == row["cost_s"] > 0


@pytest.mark.parametrize("command", [
    "aws s3 cp - s3://b/k --content-type application/json", "aws s3 cp awsbench_aws/scenarios/t05.yaml s3://b/k",
    "aws s3 cp /etc/hosts s3://b/k", "aws s3 sync . s3://b/p", "aws s3 cp s3://b/k ./k", "aws s3 mv s3://b/k /tmp/k",
    "aws s3api put-object --bucket b --key k --body x"])
def test_s3_transfers_touching_this_servers_disk_are_refused_uncharged(command):
    from awsbench_aws.server import unrunnable
    out = unrunnable("aws_cli", {"command": command})
    assert out["exit_code"] == 2 and out["charged"] is False and "PutObject" in out["stderr"]


@pytest.mark.parametrize("command", ["aws s3 cp s3://b/k -", "aws s3 cp s3://b/k - --region us-east-1",
                                     "aws s3 cp s3://a/k s3://b/k --acl private", "aws s3 ls s3://b/"])
def test_s3_transfers_between_buckets_or_to_stdout_run(command):
    from awsbench_aws.server import unrunnable
    assert unrunnable("aws_cli", {"command": command}) is None


async def test_an_upload_from_stdin_is_not_charged():
    server = api_server()
    server.bridge.run_cli = lambda command, **kw: {"exit_code": 0, "stdout": "", "stderr": ""}
    out = json.loads(await server.aws_cli("aws s3 cp - s3://vl-statuspage-assets/fallback/banner.json", SRE))
    assert out["exit_code"] == 2 and out["charged"] is False and server.meter.calls_used == 0


async def test_configure_loads_the_workspace_scenario_before_the_world_starts(monkeypatch):
    """T05R1: the world pushed its status view before scenario.load, and both pushes were dropped."""
    from test_t05_world import Aws
    server = AwsServer()
    server.ready.set()
    ops = []

    class RecWs:
        def __init__(self):
            self.dropped = []

        def health(self):
            return True

        def apply(self, op, args, at):
            ops.append(op)
            return {"ok": True, "id": "x", "deliveries": []}

    orig = server._clear

    def clear():
        orig()
        server.aws_port, server.ws_port = Aws(), RecWs()
    monkeypatch.setattr(server, "_clear", clear)
    monkeypatch.setattr(server, "_replay_seed", lambda data: 0)
    monkeypatch.setattr(server, "sweep", dict)
    out = await server.configure(scenario="t05", seed="s")
    assert ops[0] == "scenario.load" and "status.set_view" in ops and out["warnings"] == []
