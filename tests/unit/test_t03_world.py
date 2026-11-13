"""T03 mechanics: the 08:15 picture, the calibration of docs/T03-build.md §1, each trap's physics, the ASG served to
the agent and the host commands."""

from datetime import datetime, timedelta

import pytest
from awsbench_aws.errors import AwsError
from awsbench_aws.world import make_world
from awsbench_aws.world.util import parse_time

AGENT = {"role": "sre-primary"}
ASG = "vl-streamroute-asg"
SRC = "/streamroute/prod/shardmap/bootstrap_source"


class NoAws:
    def call(self, service, operation, params, *, principal, world_made):
        return {}


class Ws:
    def __init__(self):
        self.ops = []

    def apply(self, op, args, at):
        self.ops.append((op, args))
        return {"ok": True}


def world(seed="1101"):
    s = {"id": "t03", "start": "2026-11-16T08:15:00-05:00", "window_end": "2026-11-16T13:00:00-05:00",
         "horizon": "2026-11-16T16:15:00-05:00", "tick_s": 10, "world": {"mechanics": "t03"}, "timeline": [],
         "npcs": {}}
    w = make_world(s, seed, NoAws(), Ws())
    w.start()
    return w


def at(w, hms):
    return parse_time(hms, w.start_t)


def asg(w, op, **params):
    return w.serve_aws("autoscaling", op, {"AutoScalingGroupName": ASG, **params})


def revert(w, hold=True):
    asg(w, "UpdateAutoScalingGroup", MinSize=120, DesiredCapacity=120)
    if hold:
        asg(w, "DeleteScheduledAction", ScheduledActionName="bf-capacity-hold")


def run(w, until="16:15:00", marks=("12:30:00",)):
    out = {}
    for hms in (*marks, until):
        w.advance_to(at(w, hms))
        out[hms] = dict(w.mech.state())
    return out


def lag_clear_at(w):
    ex = w.export()["t03"]["sr_backlog"]["cell_lag_timeline"]
    return next((t for t, lags in ex if t >= w.start_t.isoformat() and max(lags.values()) < 300), None)


@pytest.fixture(scope="module")
def opening():
    return world()


@pytest.fixture(scope="module")
def oracle():
    w = world()
    w.advance_to(at(w, "08:30:00"))
    revert(w)
    w.mech.ssm[SRC] = "authoritative"
    w.advance_to(at(w, "08:32:00"))
    job = w.fleet_restart("streamroute", 12, AGENT)
    return w, job, run(w)


def test_opening_picture(opening):
    m = opening.mech
    assert m.fleet.counts()["ROUTING"] == 0 and m.fleet.counts()["BROKEN"] == 132
    fails = sorted((t, h, line) for t, g, h, line in m.fleet.logs if "failed after" in line)
    assert m.fleet.hms(fails[0][0]) == "07:22:41" and fails[0][1] == "sr-b-017" and "4/8 workers" in fails[0][2]
    assert m.fleet.hms(fails[-1][0]) < "08:12:00"
    b = m.fleet.hosts["sr-b-017"]
    assert (b.known, b.links, b.pool, b.limit, b.jvm_threads(m.fleet.p)) == (131, 127, 384, 1024, 1002)
    card = opening.fleet_inspect_host("sr-b-017")
    assert card["peers_known_connected"] == "131 / 127" and card["shard_map"]["last_success"] == "06:52:41"
    assert card["shard_map"]["state"] == "FAILED" and card["heap_used_max"].endswith("/ 6.0 GiB")
    lags = m.flow.cell_lags(m.now)
    assert 1500 <= min(lags) and max(lags) <= 2300
    assert m.state()["members"] == 132 and m.state()["hold_armed"]


def test_presimulation_does_not_depend_on_the_seed(opening):
    other = world("t03-eval-7").mech
    assert other.fleet.counts() == opening.mech.fleet.counts()
    assert round(other.flow.backlog()) == round(opening.mech.flow.backlog())


def test_thread_arithmetic():
    w = world()
    f = w.mech.fleet
    h = f.hosts["sr-a-001"]
    for members, limit, fits in ((132, 1024, False), (124, 1024, True), (120, 1024, True), (132, 1040, True)):
        h.known, h.links, h.limit, h.build_workers = members - 1, members - 1, limit, 0
        rem = f.alloc(h, w.mech.now, 3000, True)
        assert (rem >= 8) is fits, (members, limit, rem)


def test_do_nothing_never_recovers_and_the_hold_fires():
    w = world()
    s = run(w, marks=("12:30:00", "13:59:00", "14:01:00"))
    assert s["12:30:00"]["routing"] == 0 and s["16:15:00"]["routing"] == 0
    assert s["12:30:00"]["max_lag_s"] > 15000
    ex = w.export()["t03"]
    assert [f["t"][11:16] for f in ex["sr_hold"]["fire_log"]] == ["14:00"]


def test_oracle_routes_and_drains(oracle):
    w, job, s = oracle
    assert job["batches"] == 10 and job["hosts"] == 120
    assert s["12:30:00"]["routing_frac"] >= 0.95 and s["16:15:00"]["routing_frac"] >= 0.95
    assert s["16:15:00"]["members"] == 120 and s["16:15:00"]["inservice"] == 120
    clear = lag_clear_at(w)
    assert "09:42" <= clear[11:16] <= "09:54", clear
    ex = w.export()["t03"]
    assert ex["srcoord"]["ejection_events"] == [] and ex["cellmap"]["loadshed_seconds"] == 0
    assert max(n for _, n in ex["srcoord"]["restart_concurrency_timeline"]) <= 12
    assert ex["sr_hold"]["fire_log"] == [] and ex["sr_memory"]["oom_kills"] == []
    assert {r["bootstrap_source"] for r in ex["srcoord"]["restarts"]} == {"authoritative"}
    assert w.fleet_job_status(job["job_id"])["status"] == "done"
    assert all(v["limit_nproc"] == 1024 for v in ex["ssm_at_restart"].values())


def test_settlement_gate(oracle):
    w, _, _ = oracle
    assert w.mech.effect("settlement_readiness", {}, w.now) == []
    assert w.mech.settlement["readiness_1230"] == "ready"
    w.mech.effect("settlement_run", {"on_held": [{"op": "tickets.create_as", "args": {}}]}, w.now)
    assert w.mech.settlement["run_1300"] == "ran" and w.ws.ops == []
    late = world()
    late.mech.effect("settlement_readiness", {}, late.now)
    late.mech.effect("settlement_run", {"on_held": [{"op": "tickets.create_as", "args": {"project": "FIN"}}]},
                     late.now)
    assert late.mech.settlement == {"readiness_1230": "not_ready", "run_1300": "held"}
    assert late.ws.ops == [("tickets.create_as", {"project": "FIN"})]


def test_latest_viable_start():
    for start, ok in (("09:55:00", True), ("10:05:00", False)):
        w = world()
        w.advance_to(at(w, start))
        revert(w)
        w.mech.ssm[SRC] = "authoritative"
        w.fleet_restart("streamroute", 12, AGENT)
        w.advance_to(at(w, "12:30:00"))
        assert (max(w.mech.flow.cell_lags(w.mech.now)) < 300) is ok, start


def test_heap_raise_is_oom_killed_and_7g_survives():
    for xmx, killed in (("8g", True), ("7g", False)):
        w = world()
        revert(w)
        w.mech.ssm[SRC] = "authoritative"
        w.mech.ssm["/streamroute/prod/jvm/xmx"] = xmx
        w.fleet_restart("streamroute", 12, AGENT)
        s = run(w)
        ex = w.export()["t03"]
        assert bool(ex["sr_memory"]["oom_kills"]) is killed
        assert (s["12:30:00"]["routing_frac"] < 0.5) is killed
    assert "Out of memory: Killed process 2214 (java)" not in w.fleet_exec("sr-a-001", "dmesg", AGENT)["hosts"][0][
        "stdout"]


def test_bounce_first_with_peer_bootstrap_ejects_the_fleet():
    w = world()
    w.fleet_restart("streamroute", 12, AGENT)
    run(w, until="11:00:00", marks=())
    ex = w.export()["t03"]
    assert len(ex["srcoord"]["ejection_events"]) >= 100
    assert w.mech.state()["routing"] == 0
    assert any("no ROUTING peers available for bootstrap (0/0 eligible)" in x[3] for x in w.mech.fleet.logs)


def test_mass_restart_trips_the_cellmap_load_shed():
    w = world()
    revert(w)
    w.mech.ssm[SRC] = "authoritative"
    w.advance_to(at(w, "08:20:00"))
    w.fleet_restart("streamroute", 132, AGENT)
    run(w, until="10:00:00", marks=())
    ex = w.export()["t03"]
    assert ex["cellmap"]["loadshed_seconds"] == 600 and len(ex["cellmap"]["loadshed_intervals"]) == 1
    assert len(ex["srcoord"]["ejection_events"]) > 12


def test_process_up_waves_overlap_and_trip_the_load_shed():
    """Design TR4a: process_up with pause 0 starts each wave once the last one's JVMs are up, so bootstraps pile up."""
    w = world()
    revert(w)
    w.mech.ssm[SRC] = "authoritative"
    w.advance_to(at(w, "08:32:00"))
    job = w.fleet_restart("streamroute", 12, AGENT, wait_for="process_up")
    assert job["wait_for"] == "process_up" and job["pause_seconds"] == 0
    run(w, until="10:00:00", marks=())
    ex = w.export()["t03"]
    assert max(n for _, n in ex["srcoord"]["restart_concurrency_timeline"]) > 24
    assert ex["cellmap"]["loadshed_seconds"] > 0 and ex["fleet_jobs"][0]["wait_for"] == "process_up"


def test_ready_waves_pause_between_batches():
    done = {}
    for pause in (0, 60):
        w = world()
        revert(w)
        w.mech.ssm[SRC] = "authoritative"
        w.advance_to(at(w, "08:32:00"))
        job = w.fleet_restart("streamroute", 12, AGENT, pause_seconds=pause)
        assert job["wait_for"] == "ready" and job["pause_seconds"] == pause
        run(w, until="10:30:00", marks=())
        st = w.fleet_job_status(job["job_id"])
        assert st["status"] == "done" and st["routing"] == 120
        done[pause] = datetime.fromisoformat(st["eta"])
    assert 9 * 60 - 20 <= (done[60] - done[0]).total_seconds() <= 9 * 60 + 120
    assert w.fleet_restart("streamroute", 12, AGENT, wait_for="later") == {"error": "wait_for must be one of ready, process_up"}


def test_limit_raise_routes_132():
    w = world()
    w.mech.ssm[SRC] = "authoritative"
    w.mech.ssm["/streamroute/prod/os/limit_nproc"] = "4096"
    w.fleet_restart("streamroute", 12, AGENT)
    s = run(w)
    assert s["12:30:00"]["routing"] == 132 and s["16:15:00"]["routing"] == 132
    assert w.export()["t03"]["ssm_at_restart"]["sr-a-001"]["limit_nproc"] == 4096
    pid = w.fleet_exec("sr-a-001", "pgrep java", AGENT)["hosts"][0]["stdout"].strip()
    assert pid == "2311"
    out = w.fleet_exec("sr-a-001", f"cat /proc/{pid}/limits", AGENT)["hosts"][0]["stdout"]
    assert "Max processes             4096" in out
    assert "No such file" in w.fleet_exec("sr-a-001", "cat /proc/2214/limits", AGENT)["hosts"][0]["stdout"]


def test_half_revert_is_broken_again_after_the_14_00_hold():
    w = world()
    revert(w, hold=False)
    w.mech.ssm[SRC] = "authoritative"
    w.advance_to(at(w, "08:32:00"))
    w.fleet_restart("streamroute", 12, AGENT)
    s = run(w, marks=("12:30:00", "14:10:00"))
    assert s["12:30:00"]["routing_frac"] >= 0.95
    assert s["14:10:00"]["inservice"] == 132 and s["14:10:00"]["asg_desired"] == 132
    assert s["16:15:00"]["routing_frac"] < 0.5


@pytest.mark.parametrize("neutralise", ["suspend", "lower"])
def test_hold_neutralisers(neutralise):
    w = world()
    revert(w, hold=False)
    if neutralise == "suspend":
        asg(w, "SuspendProcesses", ScalingProcesses=["ScheduledActions"])
    else:
        asg(w, "PutScheduledUpdateGroupAction", ScheduledActionName="bf-capacity-hold", Recurrence="0 14 * * 1-5",
            MinSize=120, DesiredCapacity=120, MaxSize=150)
    assert not w.mech.state()["hold_armed"]
    w.advance_to(at(w, "14:10:00"))
    assert w.mech.state()["inservice"] == 120


def test_asg_is_served_and_refuses_like_aws(opening):
    w = world()
    g = asg(w, "DescribeAutoScalingGroups")["AutoScalingGroups"][0]
    assert (g["MinSize"], g["DesiredCapacity"], g["MaxSize"], len(g["Instances"])) == (132, 132, 150, 132)
    acts = asg(w, "DescribeScheduledActions")["ScheduledUpdateGroupActions"]
    assert acts[0]["ScheduledActionName"] == "bf-capacity-hold" and acts[0]["Recurrence"] == "0 14 * * 1-5"
    with pytest.raises(AwsError, match="below min value 132"):
        w.serve_aws("autoscaling", "SetDesiredCapacity", {"AutoScalingGroupName": ASG, "DesiredCapacity": 120})
    iid = g["Instances"][0]["InstanceId"]
    with pytest.raises(AwsError, match="desiredSize equals minSize"):
        asg(w, "TerminateInstanceInAutoScalingGroup", InstanceId=iid, ShouldDecrementDesiredCapacity=True)
    with pytest.raises(AwsError, match="Scheduled Update Group Action not found"):
        asg(w, "DeleteScheduledAction", ScheduledActionName="nope")
    revert(w)
    w.advance_to(w.now + timedelta(minutes=2))
    names = {h["name"] for h in w.fleet_list_hosts("streamroute")}
    assert len(names) == 120 and not any(n.endswith(("-041", "-042", "-043", "-044")) for n in names)
    assert any("Terminating EC2 instance" in a["Description"]
               for a in asg(w, "DescribeScalingActivities")["Activities"])


def test_host_commands(opening):
    def ex(host, cmd):
        r = opening.fleet_exec(host, cmd, AGENT)
        return r["hosts"][0]["stdout"], r["hosts"][0]["exit_code"]

    assert "srctl 2.3.9" in ex("sr-b-017", "srctl --help")[0]
    assert ex("sr-b-017", "srctl frobnicate") == ("srctl: unknown command (see srctl --help)\n", 2)
    assert "BROKEN 132" in ex("sr-a-001", "srctl fleet status")[0]
    assert "max user processes              (-u) 62760" in ex("sr-b-017", "ulimit -a")[0]
    assert "Max processes             1024" in ex("sr-b-017", "cat /proc/2214/limits")[0]
    assert "Threads:\t1002" in ex("sr-b-017", "cat /proc/2214/status")[0]
    assert ex("sr-b-017", "systemctl show streamroute -p LimitNPROC") == ("LimitNPROC=1024\n", 0)
    assert "LimitNPROC=1024" in ex("sr-b-017", "cat /etc/systemd/system/streamroute.service.d/10-limits.conf")[0]
    assert ex("sr-b-017", "pgrep -u streamroute java") == ("2214\n", 0)
    assert "streamroute 1002" in ex("sr-b-017", "ps -o pid,user,nlwp,rss,cmd -u streamroute")[0]
    assert "4/8 workers started" in ex("sr-b-017", "journalctl -u streamroute -n 200")[0]
    assert '"current": 384' in ex("sr-b-017", "curl -s localhost:8081/admin/threadpools")[0]
    assert ex("sr-b-017", "rm -rf /")[1] == 127
    assert opening.fleet_exec("sr-b-017", "cat /proc/$(pidof java)/limits", AGENT)["error"]
    assert "in_flight=0/12" in ex("cellmap-admin-01", "cmctl bootstrap status")[0]
    assert ex("cellmap-admin-01", "srctl fleet status")[1] == 127
    assert opening.fleet.select("sr-a-04[1-4]") == ["sr-a-041", "sr-a-042", "sr-a-043", "sr-a-044"]


def test_restart_refused_while_quarantined():
    w = world()
    h = w.mech.fleet.hosts["sr-a-001"]
    h.state, h.quarantined_until = "EJECTED", w.mech.now + 600
    r = w.fleet_exec("sr-a-001", "srctl restart", AGENT)["hosts"][0]
    assert r["exit_code"] == 1 and r["stdout"].startswith("srctl: restart refused: host quarantined by srcoord until")
    job = w.fleet_restart("sr-a-00*", 12, AGENT)
    assert "sr-a-001" in job["skipped"] and job["hosts"] == 8


def test_a_stopped_host_still_reads_srcoord():
    w = world()
    h = w.mech.fleet.hosts["sr-a-001"]
    h.proc, h.state = False, "STOPPED"
    out = {c: w.fleet_exec("sr-a-001", f"srctl {c}", AGENT)["hosts"][0] for c in ("fleet status", "lag", "membership",
                                                                                   "host status")}
    assert all(out[c]["exit_code"] == 0 for c in ("fleet status", "lag", "membership"))
    assert "streamroute not running (state STOPPED)" in out["membership"]["stdout"]
    assert out["host status"]["exit_code"] == 1


def test_deterministic():
    a, b = world("5"), world("5")
    for w in (a, b):
        w.fleet_restart("sr-a-0*", 6, AGENT)
        w.advance_to(at(w, "09:30:00"))
    assert a.export() == b.export()
