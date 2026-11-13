"""T01's Route 53 throttling while acme-renewer runs, epctl's retries, refusals before the meter and emulator times."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import botocore.session
import pytest
from awsbench_aws.actionlog import ActionLog
from awsbench_aws.bridge import Bridge, Faults, WallTimes, WorldPort, retime
from awsbench_aws.emulator import Emulator
from awsbench_aws.errors import AwsError
from awsbench_aws.meter import Meter, MeterConfig
from awsbench_aws.ports import AwsPort
from awsbench_aws.server import AwsServer, profiles_of
from awsbench_aws.world import World, replay_seed
from test_aws_bridge import PROFILES, ServingWorld
from test_world_t01 import LEDGER, SCENARIO, FakeAws, FakeWs, at

SRE = SimpleNamespace(role="sre-primary")
CHANGE = ("aws route53 change-resource-record-sets --hosted-zone-id Z1 --change-batch '{\"Changes\":[{\"Action\":"
          "\"UPSERT\",\"ResourceRecordSet\":{\"Name\":\"x.internal.vantleaf.io\",\"Type\":\"A\",\"TTL\":60,"
          "\"ResourceRecords\":[{\"Value\":\"10.0.0.1\"}]}}]}'")
APPLY = f"epctl apply --gen 48199 --endpoint {LEDGER} --manual --reason OPS-4452"


def codes(faults, op, times, zone="Z1"):
    out = []
    for t in times:
        try:
            faults.check("route53", op, {"HostedZoneId": f"/hostedzone/{zone}"}, at(t))
            out.append(None)
        except AwsError as e:
            out.append(e.code)
    return out


def test_t01_route53_faults_last_while_acme_runs():
    flags = {}
    f = Faults(SCENARIO["faults"], "s", flags=lambda: flags)
    assert codes(f, "ChangeResourceRecordSets", ["02:04:00", "02:41", "02:42", "02:43", "02:44"]) == [
        None, None, None, "Throttling", None]
    assert codes(f, "ChangeResourceRecordSets", ["02:44:01", "02:44:01", "02:44:04"], zone="Z2") == [
        None, "Throttling", "PriorRequestNotComplete"][:2] + [None]
    assert codes(f, "ListResourceRecordSets", [f"02:5{i}" for i in range(6)]) == [None] * 4 + ["Throttling", None]
    flags["acme_complete"] = "2026-11-17T03:38:00-05:00"
    assert codes(f, "ChangeResourceRecordSets", ["03:40:00"] * 6) == [None] * 6


def test_direct_change_is_throttled_on_the_third_call_until_acme_completes():
    world = ServingWorld()
    world.world_flags = {}
    b = Bridge(Emulator(port=1), ActionLog(), profiles=PROFILES, world=lambda: world)
    b.faults = Faults(SCENARIO["faults"], "s", flags=lambda: world.world_flags)
    runs = [b.run_cli(CHANGE, role="sre-primary", call={"t": at(f"02:5{i}:00")}) for i in range(3)]
    assert [r["exit_code"] for r in runs[:2]] == [0, 0]
    assert runs[2]["exit_code"] != 0 and "(Throttling)" in runs[2]["stderr"] and "Rate exceeded" in runs[2]["stderr"]
    assert b.actionlog.export()[-1]["error"]["Code"] == "Throttling"
    world.world_flags["acme_complete"] = "2026-11-17T03:00:00-05:00"
    assert all(b.run_cli(CHANGE, role="sre-primary", call={"t": at(f"03:1{i}:00")})["exit_code"] == 0
               for i in range(4))


@pytest.fixture
def faulted(monkeypatch):
    """The T01 world over a WorldPort whose emulator is the in-memory fake."""
    fake, box = FakeAws(), {}
    monkeypatch.setattr(AwsPort, "call", lambda self, s, o, p, *, principal, world_made, region=None,
                        log_call=None: fake.call(s, o, p, principal=principal, world_made=world_made))
    port = WorldPort(Emulator(port=1), ActionLog(), now=lambda: getattr(box.get("w"), "now", None))
    replay_seed(SCENARIO, port)
    box["w"] = w = World(SCENARIO, "eval-seed-1", port, FakeWs())
    w.start()
    port.faults = Faults(SCENARIO["faults"], "eval-seed-1", flags=lambda: w.world_flags)
    return w, port


def apply(w, port, cmd, when, call_no=9):
    w.advance_to(at(when))
    caller = {"role": "sre-primary", "principal": "assumed-role/SRE-OnCall/sre-primary", "call_no": call_no}
    port.acting = caller
    try:
        return w.fleet_exec("endpointd-enactor-a", cmd, caller)["hosts"][0], caller
    finally:
        port.acting = None


def test_epctl_apply_retries_a_throttle_and_takes_the_backoff(faulted):
    w, port = faulted
    apply(w, port, "epctl pause all", "02:49:00")
    zone = w.mirror.zone_id
    codes(port.faults, "ChangeResourceRecordSets", ["02:50:00", "02:51:00"], zone=zone)
    out, caller = apply(w, port, APPLY, "02:52:30")
    assert out["exit_code"] == 0, out["stdout"]
    assert "route53: Throttling: Rate exceeded (retry 1/3 in 2s)" in out["stdout"]
    assert caller["retry_s"] == 2 and w.endpointd.correct(LEDGER)
    (row,) = [r for r in port.actionlog.export() if r["error"]]
    assert row["error"]["Code"] == "Throttling" and row["call_no"] == 9 and row["operation"] == "ChangeResourceRecordSets"


def test_epctl_apply_all_absorbs_prior_request_not_complete(faulted):
    w, port = faulted
    apply(w, port, "epctl pause all", "02:49:00")
    out, caller = apply(w, port, "epctl apply --gen 48199 --all --manual --reason x", "02:55:00")
    assert out["exit_code"] == 0, out["stdout"]
    assert "PriorRequestNotComplete" in out["stdout"] and caller["retry_s"] >= 2 * (len(w.endpointd.order) - 1)
    assert all(w.endpointd.correct(f) for f in w.endpointd.order)
    w.world_effect("acme_complete", {}, at("02:56:00"))
    out, caller = apply(w, port, "epctl apply --gen 48199 --all --manual --reason x", "02:57:00")
    assert out["exit_code"] == 0 and "retry" not in out["stdout"] and not caller.get("retry_s")


async def test_server_charges_epctl_retries_as_time(faulted):
    w, port = faulted
    server = AwsServer()
    server.ready.set()
    server.scenario, server.world, server.aws_port = SCENARIO, w, port
    server.bridge.profiles = profiles_of(SCENARIO)
    server.meter = Meter(MeterConfig.from_scenario(SCENARIO, seed="s", mode="eval"), world=w, inbox=server.inbox,
                         actionlog=server.actionlog, world_lock=server.world_lock)
    await server.fleet_exec("endpointd-enactor-a", "epctl pause all", SRE)
    codes(port.faults, "ChangeResourceRecordSets", ["02:40:01", "02:40:02"], zone=w.mirror.zone_id)
    before = server.meter.now
    out = json.loads(await server.fleet_exec("endpointd-enactor-a", APPLY, SRE))
    assert "retry 1/3 in 2s" in out["hosts"][0]["stdout"]
    assert server.meter.now - before == timedelta(seconds=server.meter.rows[-1]["cost_s"] + 2)


@pytest.mark.parametrize("tool,args", [
    ("aws_cli", {"command": "aws route53 list-hosted-zones | head"}),
    ("aws_cli", {"command": "aws ssm get-parameter --name x; aws ssm get-parameter --name y"}),
    ("aws_cli", {"command": "aws configure list"}),
    ("aws_cli", {"command": "aws"}),
    ("fleet_exec", {"selector": "endpointd-enactor-a", "command": "epctl status; epctl verify"}),
    ("fleet_exec", {"selector": "endpointd-enactor-a", "command": "  "}),
])
async def test_unrunnable_calls_are_refused_before_the_meter(tool, args):
    server = AwsServer()
    server.ready.set()
    server.scenario = {}
    out = json.loads(await getattr(server, tool)(**args, ctx=SRE))
    assert out["exit_code"] == 2 and out["charged"] is False and out["_clock"]["call"] == 0
    assert server.meter.calls_used == 0 and server.actionlog.export() == []


async def test_a_command_that_runs_and_fails_is_still_charged():
    server = AwsServer()
    server.ready.set()
    server.scenario = {}
    out = json.loads(await server.aws_cli("aws route53 no-such-operation", SRE))
    assert out["exit_code"] != 0 and server.meter.calls_used == 1


def output_shape(service, op):
    return botocore.session.get_session().get_service_model(service).operation_model(op).output_shape


def test_retime_maps_emulator_times_to_the_virtual_clock():
    seeded = datetime.fromisoformat(SCENARIO["seeded_at"])
    times = WallTimes(seeded)
    wall = datetime.now(UTC).timestamp() - 100
    times.marks = [(wall, at("02:40:00")), (wall + 10, at("02:52:30"))]
    ssm = output_shape("ssm", "GetParameter")
    got = retime({"Parameter": {"Name": "/x", "LastModifiedDate": wall - 5.27}}, ssm, times)
    assert got["Parameter"] == {"Name": "/x", "LastModifiedDate": seeded.timestamp()}
    got = retime({"Parameter": {"LastModifiedDate": datetime.fromtimestamp(wall + 10.4, UTC)}}, ssm, times)
    assert got["Parameter"]["LastModifiedDate"] == at("02:52:30").astimezone(UTC)
    r53 = retime({"ChangeInfo": {"SubmittedAt": datetime.fromtimestamp(wall + 3, UTC).isoformat()[:23] + "Z"}},
                 output_shape("route53", "GetChange"), times)
    assert r53["ChangeInfo"]["SubmittedAt"] == "2026-11-17T07:40:00.000Z"
    logs = retime({"logGroups": [{"logGroupName": "g", "creationTime": int(wall * 1000) - 1, "storedBytes": 7}]},
                  output_shape("logs", "DescribeLogGroups"), times)
    assert logs["logGroups"][0] == {"logGroupName": "g", "creationTime": int(seeded.timestamp() * 1000),
                                    "storedBytes": 7}
    future = {"Parameter": {"LastModifiedDate": at("04:00:00").timestamp()}}
    assert retime(future, ssm, times) == future


def test_bridge_retimes_only_what_the_emulator_answered():
    b = Bridge(Emulator(port=1), ActionLog(), profiles=PROFILES)
    b.times = WallTimes(datetime.fromisoformat(SCENARIO["seeded_at"]))
    model = botocore.session.get_session().get_service_model("ssm").operation_model("GetParameter")
    for served, want in ((False, b.times.seeded_at.timestamp()), (True, 1.0)):
        parsed = {"Parameter": {"LastModifiedDate": 1.0}}
        b._after(http_response=SimpleNamespace(status_code=200), parsed=parsed, model=model,
                 context={"awsbench": {"service": "ssm", "operation": "GetParameter", "served": served}})
        assert parsed["Parameter"]["LastModifiedDate"] == want


def test_acme_renewer_logs_its_progress_until_the_run_ends(faulted):
    w, _ = faulted
    start = int(at("02:00:00").timestamp() * 1000)

    def events():
        return w.serve_aws("logs", "FilterLogEvents", {"logGroupName": "/vl/platform/acme-renewer",
                                                       "startTime": start})["events"]

    w.advance_to(at("03:20:00"))
    lines = [e["message"] for e in events()]
    assert lines[0].startswith("INFO run started: 2,412 domains") and len(lines) == 16
    assert "1,051/2,412 domains renewed" in lines[8] and "Route 53 throttled 0 calls" in lines[1]
    assert all("TXT changes" in m and "throttled" in m for m in lines[1:])
    w.advance_to(at("03:50:00"))
    done = datetime.fromisoformat(w.world_flags["acme_complete"]).timestamp() * 1000
    evs = events()
    assert evs[-1]["message"] == "INFO run complete: 2,412/2,412" and abs(evs[-1]["timestamp"] - done) < 1000
    assert all(e["timestamp"] <= done for e in evs[:-1]) and done - evs[-2]["timestamp"] <= 300_000
