import asyncio
from datetime import datetime, timedelta

import pytest
from awsbench_aws.actionlog import ActionLog
from awsbench_aws.meter import ET, Inbox, Meter, MeterConfig, classify, clock_text, jitter, whole
from test_world_t01 import SCENARIO

START = datetime(2026, 11, 17, 2, 40, tzinfo=ET)
END = datetime(2026, 11, 17, 5, 10, tzinfo=ET)
T01_OVERRIDES = [
    {"tool": "fleet_exec", "match": "epctl (status|plans list|plan show|plan diff|verify)( |$)(?!.*--resolve)",
     "class": "point_read"},
    {"tool": "fleet_exec", "match": "epctl verify .*--resolve", "class": "broad"},
    {"tool": "fleet_exec", "match": "epctl (pause|resume|apply|plan restore|set-applied)", "class": "write"},
    {"tool": "fleet_exec", "match": "unbound-control flush", "hosts": 1, "class": "write"},
    {"tool": "fleet_exec", "match": "systemctl (stop|start|restart) endpointd-", "class": "write"},
    {"tool": "fleet_exec", "hosts": ">1", "class": "broad"},
]


class FakeWorld:
    def __init__(self, deliveries=None):
        self.now = START
        self.deliveries = sorted(deliveries or [], key=lambda d: d[0])
        self.advances, self.observed = [], []

    def advance_to(self, t, wake=None):
        self.advances.append((t, wake))
        woke = []
        while self.deliveries and self.deliveries[0][0] <= t:
            at, item = self.deliveries.pop(0)
            woke.append(item)
            if wake and item["kind"] in wake:
                self.now = at
                return {"now": at, "woke": woke, "fired": ["ev-x"]}
        self.now = t
        return {"now": t, "woke": woke, "fired": ["ev-x"]}

    def observe(self, call):
        self.observed.append(call)


def meter(**kw):
    cfg = MeterConfig(start=START, window_end=kw.pop("window_end", END), horizon=END,
                      max_calls=kw.pop("max_calls", 250), seed="s", mode=kw.pop("mode", "eval"),
                      overrides=kw.pop("overrides", T01_OVERRIDES))
    return Meter(cfg, **kw)


@pytest.mark.parametrize("tool,args,hosts,cls", [
    ("aws_cli", {"command": "aws route53 list-hosted-zones"}, None, "point_read"),
    ("aws_cli", {"command": "aws --profile vl-prod/SRE-OnCall route53 test-dns-answer --hosted-zone-id Z"}, None,
     "point_read"),
    ("aws_cli", {"command": "aws route53 change-resource-record-sets --hosted-zone-id Z"}, None, "write"),
    ("aws_cli", {"command": "aws logs start-query --log-group-names g"}, None, "broad"),
    ("aws_api", {"service": "dynamodb", "operation": "PutItem"}, None, "write"),
    ("aws_api", {"service": "dynamodb", "operation": "DescribeTable"}, None, "point_read"),
    ("aws_query", {}, None, "broad"),
    ("aws_table_export", {}, None, "broad"),
    ("wait", {}, None, "free"),
    ("awsbench_control", {}, None, "free"),
    ("chat_read", {}, None, "point_read"),
    ("chat_list_channels", {}, None, "point_read"),
    ("tickets_get", {}, None, "point_read"),
    ("chat_post", {}, None, "write"),
    ("fleet_list_hosts", {}, None, "point_read"),
    ("fleet_exec", {"command": "epctl status"}, 1, "point_read"),
    ("fleet_exec", {"command": "epctl verify --resolve"}, 1, "broad"),
    ("fleet_exec", {"command": "epctl plan restore 48177"}, 1, "write"),
    ("fleet_exec", {"command": "unbound-control flush ledger"}, 1, "write"),
    ("fleet_exec", {"command": "unbound-control flush ledger"}, 3, "broad"),
    ("fleet_exec", {"command": "dig +short ledger"}, 2, "broad"),
    ("fleet_exec", {"command": "dig +short ledger"}, 1, "point_read"),
    ("fleet_exec", {"command": "epctl status"}, 4, "broad"),
])
def test_classify(tool, args, hosts, cls):
    assert classify(tool, args, T01_OVERRIDES, hosts) == cls


def test_jitter_is_deterministic_and_bounded():
    assert jitter("s", 1, "eval") == 1.0
    draws = [jitter("seed-a", k, "train") for k in range(1, 200)]
    assert draws == [jitter("seed-a", k, "train") for k in range(1, 200)]
    assert all(0.75 <= d <= 1.25 for d in draws)
    assert len(set(draws)) > 150
    assert draws != [jitter("seed-b", k, "train") for k in range(1, 200)]


async def test_costs_advance_the_world_and_clock():
    world = FakeWorld()
    m = meter(world=world)
    b = await m.begin("aws", "aws_cli", {"command": "aws route53 list-hosted-zones"}, "sre-primary")
    assert b["ok"] and b["call_no"] == 1 and b["calls_left"] == 249
    assert m.now == START + timedelta(seconds=20)
    assert world.advances[-1][0] == START + timedelta(seconds=20)
    assert m.clock.now == m.now
    e = await m.end(b["call_no"], "aws_cli", {}, None, {})
    assert e["banner"] == {"now": "02:40:20 ET", "call": 1, "calls_left": 249, "new": []}
    assert world.observed[0]["call_no"] == 1
    b = await m.begin("workspace", "chat_post", {"channel": "inc"}, "sre-primary")
    await m.end(b["call_no"], "chat_post", {}, None, {})
    assert m.now == START + timedelta(seconds=65)
    assert [r["cost_class"] for r in m.rows] == ["point_read", "write"]


async def test_uncharged_roles_cost_nothing():
    m = meter()
    b = await m.begin("aws", "aws_cli", {"command": "aws s3 ls"}, "harness")
    assert b["ok"] and not b["charged"] and b["call_no"] is None
    await m.end(None, "aws_cli", {}, None, {})
    assert m.calls_used == 0 and m.now == START


async def test_budget_refuses_without_charging():
    m = meter(max_calls=2)
    for _ in range(2):
        b = await m.begin("aws", "fleet_list_hosts", {}, "sre-primary")
        await m.end(b["call_no"], "fleet_list_hosts", {}, None, {})
    now = m.now
    b = await m.begin("aws", "fleet_list_hosts", {}, "sre-primary")
    assert b == {"ok": False, "refuse": "Call budget exhausted (2/2); write nothing more and reply DONE"}
    assert m.now == now and m.calls_used == 2
    b = await m.begin("aws", "fleet_list_hosts", {}, "harness")
    assert b["ok"]


async def test_window_end_clamps_then_refuses():
    m = meter(window_end=START + timedelta(seconds=30))
    b = await m.begin("aws", "chat_post", {}, "sre-primary")
    assert m.now == START + timedelta(seconds=30)
    await m.end(b["call_no"], "chat_post", {}, None, {})
    b = await m.begin("aws", "chat_read", {}, "sre-primary")
    assert not b["ok"] and "window closed at 02:40:30 ET" in b["refuse"]


async def test_ended_episode_refuses():
    m = meter()
    m.end_episode("done")
    b = await m.begin("aws", "chat_read", {}, "sre-primary")
    assert not b["ok"] and "ended" in b["refuse"]


async def test_lane_serializes_charged_calls():
    m = meter()
    first = await m.begin("aws", "chat_read", {}, "sre-primary")
    second = asyncio.create_task(m.begin("aws", "chat_read", {}, "sre-primary"))
    await asyncio.sleep(0.05)
    assert not second.done()
    await m.end(first["call_no"], "chat_read", {}, None, {})
    got = await asyncio.wait_for(second, 1)
    assert got["call_no"] == 2
    await m.end(got["call_no"], "chat_read", {}, None, {})


async def test_lane_lease_evicts_a_lost_end():
    m = meter()
    m.lane.lease_s = 0.1
    await m.begin("aws", "chat_read", {}, "sre-primary")
    got = await asyncio.wait_for(m.begin("aws", "chat_read", {}, "sre-primary"), 2)
    assert got["call_no"] == 2


async def test_banner_carries_new_inbound_once():
    item = {"t": "x", "kind": "dm", "from": "hannah.becker", "where": "dm", "preview": "hi"}
    world = FakeWorld([(START + timedelta(seconds=10), item)])
    m = meter(world=world, inbox=Inbox())
    b = await m.begin("aws", "chat_read", {}, "sre-primary")
    e = await m.end(b["call_no"], "chat_read", {}, None, {})
    assert e["banner"]["new"] == [item]
    b = await m.begin("aws", "chat_read", {}, "sre-primary")
    e = await m.end(b["call_no"], "chat_read", {}, None, {})
    assert e["banner"]["new"] == []


async def test_wait_wakes_early_on_inbound():
    dm = {"t": "x", "kind": "dm", "from": "callum.reyes", "where": "dm", "preview": "status?"}
    world = FakeWorld([(START + timedelta(minutes=4), {"t": "y", "kind": "channel", "from": "a", "where": "#x",
                                                       "preview": "noise"}),
                       (START + timedelta(minutes=7), dm)])
    m = meter(world=world)
    out = await m.wait(START + timedelta(minutes=20))
    assert out["woke_early"] and out["reason"] == "inbound"
    assert out["waited_s"] == 420
    assert dm in out["inbound"]


async def test_wait_shows_only_its_documented_keys():
    keys = {"waited_s", "now", "woke_early", "reason", "inbound"}
    dm = {"t": "x", "kind": "dm", "from": "callum.reyes", "where": "dm", "preview": "status?"}
    early = await meter(world=FakeWorld([(START + timedelta(minutes=2), dm)])).wait(START + timedelta(minutes=10))
    full = await meter(world=FakeWorld()).wait(START + timedelta(minutes=10))
    assert early["reason"] == "inbound" and full["reason"] == "until_reached"
    assert set(early) == keys and set(full) == keys


async def test_wait_is_clamped_to_thirty_minutes_and_window():
    m = meter()
    out = await m.wait(START + timedelta(hours=2))
    assert out["waited_s"] == 1800 and out["reason"] == "until_reached"
    m = meter(window_end=START + timedelta(minutes=5))
    out = await m.wait(START + timedelta(minutes=20))
    assert out["waited_s"] == 300 and out["reason"] == "window_end"


async def test_end_logs_non_aws_tool_calls():
    log = ActionLog()
    m = meter(actionlog=log)
    b = await m.begin("workspace", "chat_post", {"channel": "inc-4452"}, "sre-primary")
    await m.end(b["call_no"], "chat_post", {"channel": "inc-4452"}, None, {})
    b = await m.begin("aws", "aws_cli", {"command": "aws s3 ls"}, "sre-primary")
    await m.end(b["call_no"], "aws_cli", {}, None, {"logged": True})
    rows = log.export()
    assert [r["tool"] for r in rows] == ["chat_post"]
    assert rows[0]["call_no"] == 1 and rows[0]["t"] == (START + timedelta(seconds=45)).isoformat()


def test_clock_text():
    assert clock_text(START) == "02:40:00 ET"


def test_config_from_scenario():
    cfg = MeterConfig.from_scenario({"start": "2026-11-17T02:40:00-05:00", "window_end": "2026-11-17T05:10:00-05:00",
                                     "budget": {"max_calls": 250}, "clock": {"overrides": T01_OVERRIDES, "wait": {
                                         "max_minutes": 30, "early_wake": ["dm", "email_to_role", "job_done"]}}},
                                    seed="7", mode="train")
    assert cfg.max_calls == 250 and cfg.wake == {"dm", "email", "job_done"} and cfg.horizon == cfg.window_end
    assert cfg.charged_roles == ("sre-primary",) and cfg.overrides == T01_OVERRIDES


async def test_an_early_wake_between_seconds_lands_on_the_next_whole_second():
    dm = {"t": "x", "kind": "dm", "from": "hannah.becker", "where": "dm", "preview": "ETA?"}
    at = START + timedelta(minutes=7, microseconds=110954)
    world = FakeWorld([(at, dm)])
    m = meter(world=world)
    out = await m.wait(START + timedelta(minutes=20))
    assert out["reason"] == "inbound" and out["woke_early"] and dm in out["inbound"]
    assert m.now == START + timedelta(minutes=7, seconds=1) == world.now and m.clock.now == m.now
    assert out["waited_s"] == 421 and out["now"] == "2026-11-17T02:47:01-05:00"
    b = await m.begin("aws", "chat_read", {}, "sre-primary")
    assert b["t_exec"] == "2026-11-17T02:47:21-05:00"


async def test_train_jitter_keeps_times_in_whole_seconds():
    m = meter(mode="train")
    for _ in range(20):
        b = await m.begin("aws", "chat_post", {}, "sre-primary")
        await m.end(b["call_no"], "chat_post", {}, None, {})
        assert m.now.microsecond == 0
    assert all(isinstance(r["cost_s"], int) for r in m.rows) and len({r["cost_s"] for r in m.rows}) > 1


def test_whole_rounds_up_only_fractions():
    assert whole(START) == START
    assert whole(START + timedelta(microseconds=1)) == START + timedelta(seconds=1)


async def test_end_logs_a_fleet_exit_code():
    log = ActionLog()
    m = meter(actionlog=log)
    b = await m.begin("aws", "fleet_exec", {"command": "epctl apply --gen 48177"}, "sre-primary", hosts=1)
    await m.end(b["call_no"], "fleet_exec", {}, {"error": "exit code 1 on ep-enactor-a"},
                {"error": "exit code 1 on ep-enactor-a", "exit_code": 1})
    (row,) = log.export()
    assert row["exit_code"] == 1 and row["error"] == "exit code 1 on ep-enactor-a"


@pytest.mark.parametrize("tool", ["chat_get_user", "chat_read", "chat_search", "chat_list_channels", "mail_read",
                                  "mail_search", "pager_get", "pager_list", "pager_oncall", "repo_get", "repo_list",
                                  "repo_log", "repo_read", "repo_search", "status_get", "tickets_get", "tickets_search",
                                  "wiki_get", "wiki_history", "wiki_search"])
def test_workspace_reads_are_point_reads(tool):
    assert classify(tool, {}, T01_OVERRIDES, None) == "point_read"


@pytest.mark.parametrize("tool", ["chat_post", "tickets_comment", "pager_page", "status_update"])
def test_workspace_writes_are_writes(tool):
    assert classify(tool, {}, T01_OVERRIDES, None) == "write"


def test_t01_charges_epctl_help_as_a_read():
    overrides = SCENARIO["cost_overrides"]
    for command in ("epctl apply --gen 48199 --help", "epctl resume all -h", "epctl help set-applied", "epctl --help"):
        assert classify("fleet_exec", {"command": command}, overrides, 1) == "point_read", command
    assert classify("fleet_exec", {"command": "epctl apply --gen 48199 --reason help"}, overrides, 1) == "write"


class JobWorld(FakeWorld):
    """A world whose job_done comes back in `delivered`, and in `woke` only when an advance stops on it, as the real
    worlds' do."""

    def advance_to(self, t, wake=None):
        self.advances.append((t, wake))
        out = []
        while self.deliveries and self.deliveries[0][0] <= t:
            at, item = self.deliveries.pop(0)
            out.append(item)
            if wake and item["kind"] in wake:
                self.now = at
                return {"now": at, "woke": out, "fired": [], "delivered": out}
        self.now = t
        return {"now": t, "woke": [], "fired": [], "delivered": out}


JOB = {"t": "02:40:10 ET", "kind": "job_done", "from": "fleet", "where": "job-0001", "preview": "restart done"}


async def test_a_job_done_inside_a_charged_call_reaches_the_banner_once():
    m = meter(world=JobWorld([(START + timedelta(seconds=10), JOB)]))
    b = await m.begin("aws", "fleet_job_status", {"job_id": "job-0001"}, "sre-primary")
    assert (await m.end(b["call_no"], "fleet_job_status", {}, None, {}))["banner"]["new"] == [JOB]
    b = await m.begin("aws", "fleet_job_status", {"job_id": "job-0001"}, "sre-primary")
    assert (await m.end(b["call_no"], "fleet_job_status", {}, None, {}))["banner"]["new"] == []


async def test_a_job_done_that_wakes_a_wait_is_listed_once():
    out = await meter(world=JobWorld([(START + timedelta(minutes=2), JOB)])).wait(START + timedelta(minutes=10))
    assert out["inbound"] == [JOB] and out["woke_early"]


@pytest.mark.parametrize("command,cls", [
    ("aws s3 cp s3://b/k -", "point_read"), ("aws s3 presign s3://b/k", "point_read"),
    ("aws --profile vl-prod/SRE-OnCall s3 cp s3://b/k - --region us-east-1", "point_read"),
    ("aws s3 cp s3://a/k s3://b/k", "write"), ("aws s3 rm s3://b/k", "write"), ("aws s3 ls s3://b/", "point_read")])
def test_an_s3_download_to_stdout_is_a_read(command, cls):
    assert classify("aws_cli", {"command": command}) == cls


def test_t05_describe_instances_is_broad_unless_filtered_by_name_pool_or_role():
    from awsbench_aws.world import load_scenario
    over = load_scenario("t05")["cost_overrides"]
    tagged = "aws ec2 describe-instances --filters Name=tag:aws:autoscaling:groupName,Values=x"
    assert classify("aws_cli", {"command": tagged}, over) == "broad"
    pool = "aws ec2 describe-instances --filters Name=tag:Pool,Values=vs-blobindex"
    assert classify("aws_cli", {"command": pool}, over) == "point_read"
