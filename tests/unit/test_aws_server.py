from datetime import datetime
from types import SimpleNamespace

import pytest
from awsbench_aws.cloudtrail import event
from awsbench_aws.meter import parse_time
from awsbench_aws.server import NO_SCENARIO, AwsServer, count_hosts, fleet_outcome, lookup_attributes_of, profiles_of
from awsbench_aws.world import World, replay_seed
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
