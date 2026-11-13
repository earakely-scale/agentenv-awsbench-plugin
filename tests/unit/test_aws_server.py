import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from awsbench_aws.account import ACCOUNT, Shown
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
