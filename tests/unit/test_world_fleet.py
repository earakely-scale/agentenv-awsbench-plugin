"""fleet_exec refuses shell operators and answers the common read commands on every host; the ingress ASG and the
outbox table exist."""

import json

import pytest
from awsbench_aws.errors import AwsError
from awsbench_aws.world import World, replay_seed
from awsbench_aws.world.epctl import USAGES
from awsbench_aws.world.fleet import ONE_COMMAND
from test_world_t01 import AGENT, LEDGER, SCENARIO, FakeAws, FakeWs, at, ep

ASG = "vl-ledger-ingress-asg"


@pytest.fixture
def world():
    aws, ws = FakeAws(), FakeWs()
    replay_seed(SCENARIO, aws)
    w = World(SCENARIO, "eval-seed-1", aws, ws)
    w.start()
    return w


def run(w, host, cmd):
    out = w.fleet_exec(host, cmd, AGENT)["hosts"][0]
    return out["stdout"], out["exit_code"]


@pytest.mark.parametrize("cmd", ["epctl status; epctl verify", f"dig +short {LEDGER}; dig {LEDGER} | grep status",
                                 "uptime;", "journalctl -u unbound 2>&1", "ps aux | head", "echo $(id)",
                                 "uptime && date", "cat /etc/resolv.conf > /tmp/x", "echo `id`"])
def test_shell_operators_are_refused(world, cmd):
    assert world.fleet_exec("endpointd-enactor-b", cmd, AGENT) == {"hosts": [], "exit_code": 2, "error": ONE_COMMAND}


@pytest.mark.parametrize("host", ["core-a-01", "endpointd-enactor-b", "endpointd-planner-1", "li-b-01", "lw-c-04"])
def test_read_commands_work_on_every_host(world, host):
    world.advance_to(at("02:45:00"))
    for cmd in ("uptime", "ps", "ps aux", "ps -ef", "systemctl status unbound", "journalctl -u unbound -n 5",
                "hostname", "date"):
        out, code = run(world, host, cmd)
        assert code == 0 and out.strip() and "not available" not in out, (host, cmd, out)
    assert "USER" in run(world, host, "ps aux")[0]
    assert run(world, host, "systemctl status nope")[1] == 4


def test_systemctl_and_curl_reflect_the_outage(world):
    world.advance_to(at("02:45:00"))
    out, code = run(world, "core-a-01", "systemctl status checkout-api")
    assert code == 0 and "active (running)" in out and "CircuitBreakerOpenException" in out
    assert "UnknownHostException" in run(world, "core-a-01", "journalctl -u checkout-api")[0]
    body = json.loads(run(world, "core-a-01", "curl -s localhost:8080/health")[0])
    assert body["status"] == "DOWN" and "UnknownHostException" in body["components"]["ledger"]["error"]
    assert run(world, "core-a-01", "curl -sf localhost:8080/health")[1] == 22
    assert run(world, "core-a-01", "curl -s localhost:9999/health")[1] == 7
    assert run(world, "core-a-01", f"curl -s https://{LEDGER}/health")[1] == 6
    assert run(world, "li-b-01", "curl -s localhost:9901/ready")[0] == "PRE_INITIALIZING\n"
    assert run(world, "li-a-01", "curl -s localhost:9901/ready")[0] == "LIVE\n"
    assert "NOT READY" in run(world, "li-b-01", "systemctl status envoy")[0]
    assert json.loads(run(world, "endpointd-enactor-a", "curl -s localhost:9100/health")[0])["state"].startswith(
        "HALTED")
    assert "ledger-writer" in run(world, "lw-a-01", "ps aux")[0]


def test_journal_shows_the_outage_then_the_storm(world):
    world.advance_to(at("02:45:00"))
    out, _ = run(world, "core-a-01", "journalctl -u checkout-api -n 12")
    assert out.count("\n") == 12 and out.count("UnknownHostException") == 1 and "core-a-01 checkout-api[3011]" in out
    assert "inbound" in run(world, "lw-a-01", "journalctl -u ledger-writer -n 3")[0]
    assert "NXDOMAIN" in run(world, "core-a-01", "journalctl -u unbound")[0]
    assert "halt latch engaged" in run(world, "endpointd-enactor-b", "journalctl -u endpointd-enactor-b")[0]
    ep(world, "epctl pause all", "02:49:00")
    ep(world, f"epctl apply --gen 48199 --endpoint {LEDGER} --manual --reason x", "02:52:30")
    world.advance_to(at("03:12:00"))
    assert world.storm.regime == "collapsed"
    out, _ = run(world, "lw-a-01", "journalctl -u ledger-writer -n 6")
    assert "db pool exhausted" in out and "inbound" not in out.splitlines()[-1]
    assert "SocketTimeoutException" in run(world, "core-b-02", "journalctl -u checkout-api -n 4")[0]
    assert json.loads(run(world, "lw-a-01", "curl -s localhost:8090/health")[0])["status"] == "DEGRADED"


def test_ingress_asg_and_its_paused_refresh(world):
    world.advance_to(at("02:45:00"))
    grp = world.serve_aws("autoscaling", "DescribeAutoScalingGroups", {"AutoScalingGroupNames": [ASG]})
    g = grp["AutoScalingGroups"][0]
    assert g["DesiredCapacity"] == 6 and len(g["Instances"]) == 6
    assert world.serve_aws("autoscaling", "DescribeAutoScalingGroups",
                           {"AutoScalingGroupNames": ["other"]}) == {"AutoScalingGroups": []}
    ref = world.serve_aws("autoscaling", "DescribeInstanceRefreshes", {"AutoScalingGroupName": ASG})
    r = ref["InstanceRefreshes"][0]
    assert r["Status"] == "InProgress" and "13:21:00Z" in r["StatusReason"] and r["PercentageComplete"] == 50
    world.advance_to(at("02:56:10"))
    assert world.serve_aws("autoscaling", "CancelInstanceRefresh", {"AutoScalingGroupName": ASG}) == {
        "InstanceRefreshId": r["InstanceRefreshId"]}
    status = lambda: world.serve_aws("autoscaling", "DescribeInstanceRefreshes",
                                     {"AutoScalingGroupName": ASG})["InstanceRefreshes"][0]["Status"]
    assert status() == "Cancelling"
    world.advance_to(at("02:57:00"))
    assert status() == "Cancelled"
    with pytest.raises(AwsError) as e:
        world.serve_aws("autoscaling", "CancelInstanceRefresh", {"AutoScalingGroupName": ASG})
    assert e.value.code == "ActiveInstanceRefreshNotFound"
    exp = world.export()["ingress_asg"]
    assert exp["refresh_status"] == "Cancelled" and exp["held"] and exp["cancelled_at"].startswith(
        "2026-11-17T02:56:10")
    assert world.serve_aws("autoscaling", "DescribeScalingActivities", {"AutoScalingGroupName": "x"}) is None


def test_untouched_refresh_is_exported_as_pending(world):
    world.advance_to(at("05:10:00"))
    exp = world.export()["ingress_asg"]
    assert exp["refresh_status"] == "InProgress" and not exp["held"]
    assert exp["resumes_at"] == "2026-11-17T08:21:00-05:00"


def test_seed_creates_the_outbox_table(world):
    assert "vl-ledger-outbox" in world.aws.tables


def test_fleet_output_is_deterministic():
    def outs():
        aws, ws = FakeAws(), FakeWs()
        replay_seed(SCENARIO, aws)
        w = World(SCENARIO, "eval-seed-1", aws, ws)
        w.start()
        w.advance_to(at("02:47:00"))
        return [w.fleet_exec(h, c, AGENT) for h in ("core-a-01", "lw-b-02", "li-c-01")
                for c in ("ps aux", "journalctl -n 30", "systemctl status unbound", "uptime")]
    assert outs() == outs()


def test_sudo_is_stripped_since_hosts_run_as_root(world):
    host = next(h for h, v in world.fleet.hosts.items() if v["fleet"] == "vl-core")
    plain = run(world, host, f"unbound-control flush {LEDGER}")
    assert run(world, host, f"sudo unbound-control flush {LEDGER}")[1] == plain[1] == 0
    assert "not available" not in run(world, host, f"sudo dig +short {LEDGER}")[0]


@pytest.mark.parametrize("host,cmd,code,want", [
    ("core-a-01", "systemctl status checkout-api --no-pager", 0, "Loaded:"),
    ("core-a-01", "systemctl --no-pager status checkout-api", 0, "Loaded:"),
    ("endpointd-enactor-b", "systemctl status endpointd-enactor-b --no-pager -n 5", 0, "Loaded:"),
    ("endpointd-enactor-b", "systemctl list-units --type=service", 0, "endpointd-enactor-b.service"),
    ("endpointd-enactor-b", "systemctl is-active endpointd-enactor-b", 0, "active"),
    ("core-a-01", "systemctl status nope --no-pager", 4, "Unit nope.service could not be found"),
])
def test_systemctl_reads_flags_as_options(world, host, cmd, code, want):
    world.advance_to(at("02:45:00"))
    out, rc = run(world, host, cmd)
    assert rc == code and want in out and "--no-pager" not in out and "--type" not in out


def test_systemctl_status_of_several_units_and_stop_with_a_flag(world):
    out, rc = run(world, "core-a-01", "systemctl status unbound checkout-api")
    assert rc == 0 and "unbound.service" in out and "checkout-api.service" in out
    out, rc = run(world, "endpointd-enactor-b", "systemctl stop endpointd-enactor-b --no-block")
    assert rc == 0 and world.endpointd.processes["enactor-b"] is False
    out, rc = run(world, "endpointd-enactor-b", "systemctl is-active endpointd-enactor-b.service")
    assert (out, rc) == ("inactive\n", 3)


def test_inspect_shows_services_cache_and_recent_log(world):
    world.advance_to(at("02:45:00"))
    core = world.fleet_inspect_host("core-a-01")
    assert core["services"]["unbound"] == "active (running)"
    assert core["resolver"]["cache"][LEDGER].startswith("NXDOMAIN (cached negative answer")
    assert 0 < len(core["recent_log"]) <= 5
    assert "envoy" in world.fleet_inspect_host("li-b-01")["services"]
    enactor = world.fleet_inspect_host("endpointd-enactor-a")
    assert enactor["services"]["endpointd-enactor-a"] == "active (running)" and enactor["kill_switch"] == "on"


def test_scheduled_actions_are_served_empty(world):
    for p in ({"AutoScalingGroupName": ASG}, {}):
        assert world.serve_aws("autoscaling", "DescribeScheduledActions", p) == {"ScheduledUpdateGroupActions": []}


def test_refresh_matches_plat_405(world):
    r = world.serve_aws("autoscaling", "DescribeInstanceRefreshes", {"AutoScalingGroupName": ASG})["InstanceRefreshes"][0]
    assert r["Preferences"]["MinHealthyPercentage"] == 80
    assert r["DesiredConfiguration"]["LaunchTemplate"]["Version"] == "15"
    assert r["StartTime"] <= at("01:40:15")
    g = world.serve_aws("autoscaling", "DescribeAutoScalingGroups", {})["AutoScalingGroups"][0]
    versions = {i["InstanceType"][:3]: i["LaunchTemplate"]["Version"] for i in g["Instances"]}
    assert versions == {"m7g": "15", "m6i": "14"} and g["LaunchTemplate"]["Version"] == "15"


@pytest.mark.parametrize("selector,n", [("vl-core,endpointd-enactor-a", 31), ("vl-core:use1-az6", 10),
                                        ("vl-core endpointd-enactor-a", 31), ("*", None), ("all", None)])
def test_selectors_resolve_fleets_azs_and_globs_inside_lists(world, selector, n):
    hosts = world.fleet.select(selector)
    assert len(hosts) == (n or len(world.fleet.hosts))
    assert hosts == [h for h in world.fleet.hosts if h in hosts]


def test_an_az_selects_its_hosts_across_fleets(world):
    az6 = [h for h, v in world.fleet.hosts.items() if v["az"] == "use1-az6"]
    assert len({world.fleet.hosts[h]["fleet"] for h in az6}) > 1
    assert world.fleet.select("use1-az6") == az6


def test_unmatched_tokens_are_reported_not_refused(world):
    res = world.fleet_exec("vl-core,nope-*", "hostname", AGENT)
    assert len(res["hosts"]) == 30 and res["unmatched"] == ["nope-*"]
    assert "unmatched" not in world.fleet_exec("vl-core", "hostname", AGENT)


def test_unsupported_unbound_control_names_its_subcommand(world):
    out, code = run(world, "core-a-01", f"unbound-control lookup {LEDGER}")
    assert code == 1 and "lookup" in out and "flush" in out and "not available" in out
    assert run(world, "core-a-01", "unbound-control")[0].startswith("unbound-control with no command")
    assert run(world, "core-a-01", f"unbound-control flush {LEDGER}") == ("ok\n", 0)


def test_inspect_cache_holds_the_one_record_dig_answers(world):
    ep(world, "epctl pause all", "02:49:00")
    ep(world, f"epctl apply --gen 48199 --endpoint {LEDGER} --manual --reason x", "02:52:30")
    world.advance_to(at("02:53:00"))
    world.fleet_exec("core-a-01", f"unbound-control flush {LEDGER}", AGENT)
    world.advance_to(at("02:53:20"))
    cache = world.fleet_inspect_host("core-a-01")["resolver"]["cache"][LEDGER]
    ip = run(world, "core-a-01", f"dig +short {LEDGER}")[0].strip()
    assert ip and "\n" not in ip and cache.startswith(f"{ip} (ttl ")


def test_every_epctl_subcommand_prints_its_usage_on_help_and_writes_nothing(world):
    world.advance_to(at("02:50:00"))
    writes = len(world.aws.calls)
    for name, lines in USAGES.items():
        for flag in ("--help", "-h"):
            out, code = run(world, "endpointd-enactor-a", f"epctl {name} {flag}")
            assert code == 0 and out == "usage:\n" + "".join(f"  {u}\n" for u in lines), (name, flag, out)
    out, code = run(world, "endpointd-enactor-a", f"epctl apply --gen 48199 --endpoint {LEDGER} --manual --help")
    assert code == 0 and out.startswith("usage:\n  epctl apply --gen GEN")
    assert "plan restore" in run(world, "endpointd-enactor-a", "epctl plan --help")[0]
    assert run(world, "endpointd-enactor-a", "epctl --help")[0].startswith("epctl 2.3.0")
    assert run(world, "endpointd-enactor-a", "epctl nope --help")[1] == 2
    assert not [c for c in world.aws.calls[writes:] if not c[1].startswith(("Get", "List", "Describe", "Query", "Scan"))]


def test_list_hosts_refuses_an_unknown_group_or_zone_and_takes_zone_names(world):
    with pytest.raises(ValueError, match="unknown fleet 'checkout-api'; fleets: .*vl-core"):
        world.fleet_list_hosts(fleet="checkout-api")
    with pytest.raises(ValueError, match=r"unknown availability zone 'use1-az9'; zones: .*use1-az6 \(us-east-1a\)"):
        world.fleet_list_hosts(az="use1-az9")
    assert world.fleet_list_hosts(az="us-east-1a") == world.fleet_list_hosts(az="use1-az6")
    assert world.fleet_list_hosts(fleet="vl-core", az="us-east-1b") == world.fleet_list_hosts("vl-core", "use1-az1")
    ingress = world.fleet_list_hosts(fleet="ledger-ingress")
    assert len(ingress) == 6 and all("ready" in h for h in ingress)
    assert world.fleet.select("vl-core:us-east-1c") == world.fleet.select("vl-core:use1-az4")


def test_graviton_ingress_hosts_run_al2023_arm64(world):
    li = world.fleet_inspect_host("li-b-01")
    assert "Amazon Linux 2023" in li["os"] and li["arch"] == "aarch64"
    li = world.fleet_inspect_host("li-a-02")
    assert li["os"].startswith("Ubuntu") and li["arch"] == "x86_64"


def test_a_cached_answer_counts_down_its_ttl(world):
    """R10-opus-1: getent on core-b-08 gave 10.20.11.38 at 03:06:40 and inspect showed 10.20.76.30 '(ttl 60s)' 20 s
    later. A host keeps one weighted pick until its TTL runs out and shows the time left."""
    ep(world, f"epctl apply --gen 48199 --endpoint {LEDGER} --manual --reason x", "02:52:30")
    world.advance_to(at("02:58:00"))
    run(world, "core-a-01", f"unbound-control flush {LEDGER}")
    world.advance_to(at("02:58:40"))
    answer = [x for x in run(world, "core-a-01", f"dig {LEDGER}")[0].splitlines() if x.startswith(LEDGER + ".")]
    assert answer and answer[0].split()[1] == "20"
    ip = answer[0].split()[-1]
    assert world.fleet_inspect_host("core-a-01")["resolver"]["cache"][LEDGER] == f"{ip} (ttl 20s)"
    assert run(world, "core-a-01", f"dig +short {LEDGER}")[0] == ip + "\n"
    direct = [x for x in run(world, "core-a-01", f"dig @10.20.0.2 {LEDGER}")[0].splitlines()
              if x.startswith(LEDGER + ".")]
    assert direct[0].split()[1] == "60"
    world.advance_to(at("02:59:00"))
    fresh = run(world, "core-a-01", f"dig +short {LEDGER}")[0].strip()
    assert world.fleet_inspect_host("core-a-01")["resolver"]["cache"][LEDGER] == f"{fresh} (ttl 60s)"


def test_li_b_01_tells_one_story_the_sds_reload_missed_the_cert_path(world):
    """The journal and envoy-certcheck say what the planner log and PLAT-405 say: SDS couldn't load
    /etc/envoy/certs/ledger-ingress.pem, which the arm64 bootstrap writes under /etc/envoy/tls."""
    world.advance_to(at("02:50:00"))
    check, journal = run(world, "li-b-01", "envoy-certcheck"), run(world, "li-b-01", "journalctl -u envoy")
    for out, _ in (check, journal):
        assert "/etc/envoy/certs/ledger-ingress.pem" in out and "/var/lib/certsync" not in out
    assert check[1] == 1 and run(world, "li-a-01", "envoy-certcheck") == (
        "OK sds secret ingress-tls loaded; listener ledger_https ready\n", 0)
