"""T03 host commands as an operator types them: several paths, systemctl properties, ps options, the journal and
fleet_list_hosts' refusals."""

import pytest
from awsbench_aws.server import fleet_outcome
from test_t03_telemetry import world as scenario_world
from test_t03_world import AGENT, SRC, at, revert, world


def sh(w, command, host="sr-c-040"):
    (out,) = w.fleet_exec(host, command, AGENT)["hosts"]
    return out["stdout"], out["exit_code"]


@pytest.fixture(scope="module")
def opening():
    return world()


def test_cat_answers_every_path(opening):
    out, code = sh(opening, "cat /etc/sr-metrics-agent.yaml /nope /proc/2214/status")
    assert code == 1 and out.startswith("namespace: Vantleaf/Streamroute")
    assert "cat: /nope: No such file or directory\n" in out and "Threads:\t1002" in out
    assert sh(opening, "cat /etc/sr-metrics-agent.yaml /proc/2214/limits")[1] == 0


def test_systemctl_show_prints_each_property(opening):
    assert sh(opening, "systemctl show streamroute -p TasksMax,TasksCurrent,LimitNPROC") == (
        "TasksMax=18828\nTasksCurrent=1002\nLimitNPROC=1024\n", 0)
    assert sh(opening, "systemctl show -p TasksCurrent") == ("TasksCurrent=1002\n", 0)
    assert sh(opening, "systemctl show streamroute -p NoSuchThing") == ("", 0)


def test_ps_options(opening):
    out, code = sh(opening, "ps --no-headers -u streamroute -o pid,nlwp,comm")
    assert code == 0 and out.split() == ["1187", "22", "sr-metrics-agent", "2214", "1002", "java"]
    assert sh(opening, "ps -ef")[0].startswith("    PID USER")
    out, code = sh(opening, "ps -eLf --no-headers -u streamroute")
    assert code == 0 and len(out.splitlines()) == 1024 and out.splitlines()[-1].split()[:4] == [
        "2214", "3215", "streamroute", "1002"]
    assert sh(opening, "ps -o pid,wchan")[1] == 1


def test_fleet_list_hosts_names_the_valid_values(opening):
    with pytest.raises(ValueError, match="fleets: cellmap-admin, streamroute"):
        opening.fleet_list_hosts("ledger-writer")
    with pytest.raises(ValueError, match="zones: us-east-1a, us-east-1b, us-east-1c"):
        opening.fleet_list_hosts(None, "use1-az1")
    assert len(opening.fleet_list_hosts("streamroute")) == 132
    assert {h["az"] for h in opening.fleet_list_hosts(None, "us-east-1a")} == {"us-east-1a"}


def test_the_journal_follows_a_restart_and_a_wedged_host_cites_its_build():
    w = world()
    w.advance_to(at(w, "08:40:00"))
    assert "rebuild skipped: build already in progress (epoch 204412)" in sh(w, "journalctl -u streamroute")[0]
    revert(w)
    w.mech.ssm[SRC] = "authoritative"
    w.fleet_restart("sr-c-040", 1, AGENT)
    w.advance_to(at(w, "08:50:00"))
    out = sh(w, "journalctl -u streamroute -n 8")[0]
    assert "systemd[1]: Started streamroute.service" in out and "bootstrap source=authoritative" in out
    assert "ROUTING (bootstrap=authoritative)" in out


def test_thread_ceiling_files_agree_with_sysctl_and_systemd(opening):
    """sonnet-2 CALL 27: `cat /proc/sys/kernel/threads-max /sys/fs/cgroup/pids.max` said 'No such file' for both."""
    out, code = sh(opening, "cat /proc/sys/kernel/threads-max /sys/fs/cgroup/pids.max")
    assert code == 1 and out == "125520\ncat: /sys/fs/cgroup/pids.max: No such file or directory\n"
    assert sh(opening, "sysctl kernel.threads-max") == ("kernel.threads-max = 125520\n", 0)
    unit = "/sys/fs/cgroup/system.slice/streamroute.service"
    assert sh(opening, f"cat {unit}/pids.max {unit}/pids.current") == ("18828\n1002\n", 0)


def test_files_list_and_tail_like_a_host(opening):
    """sonnet-1 CALL 18 and sonnet-2 CALL 30: tail and ls answered 'not available in this environment'."""
    assert sh(opening, "ls /var/log/streamroute") == (
        "ls: cannot access '/var/log/streamroute': No such file or directory\n", 2)
    assert sh(opening, "tail -n 40 /var/log/streamroute/streamroute.log") == (
        "tail: cannot open '/var/log/streamroute/streamroute.log' for reading: No such file or directory\n", 1)
    assert sh(opening, "ls /etc/systemd/system/streamroute.service.d") == ("10-limits.conf\n", 0)
    assert sh(opening, "head -n 2 /etc/streamroute/streamroute.conf")[0].count("\n") == 2
    assert sh(opening, "grep LimitNPROC /etc/systemd/system/streamroute.service.d/10-limits.conf") == (
        "LimitNPROC=1024\n", 0)


def test_srctl_is_a_streamroute_tool(opening):
    """sonnet-1 CALL 16: `srctl fleet status` on cellmap-admin-01 said 'not available in this environment', and the
    agent filed srctl as missing for responders."""
    assert sh(opening, "srctl fleet status", "cellmap-admin-01") == ("bash: srctl: command not found\n", 127)
    assert sh(opening, "cmctl bootstrap status")[1] == 127
    assert sh(opening, "srctl fleet status")[0].startswith("members=132")


def test_pgrep_counts_the_process_table(opening):
    """opus-3 CALL 135: `pgrep -c -w -u streamroute` printed nothing and exited 1 while java ran 1002 threads."""
    assert sh(opening, "pgrep -c -w -u streamroute") == ("1024\n", 0)
    assert sh(opening, "pgrep -c -u streamroute") == ("2\n", 0)
    assert sh(opening, "pgrep java") == ("2214\n", 0)
    assert sh(opening, "pgrep -f streamroute-2.3.9.jar") == ("2214\n", 0)
    assert sh(opening, "pidof java") == ("2214\n", 0)
    assert sh(opening, "pgrep nginx") == ("", 1)


def test_each_unit_shows_its_own_properties(opening):
    """opus-3 CALL 136: `systemctl show sr-metrics-agent -p TasksCurrent -p User` gave the JVM's 986 tasks."""
    assert sh(opening, "systemctl show sr-metrics-agent -p TasksCurrent -p User") == (
        "TasksCurrent=22\nUser=streamroute\n", 0)
    assert sh(opening, "systemctl show sr-metrics-agent -p MainPID") == ("MainPID=1187\n", 0)
    assert sh(opening, "systemctl show sr-apply-limits -p Type,ActiveState,TasksCurrent") == (
        "Type=oneshot\nActiveState=inactive\nTasksCurrent=[not set]\n", 0)


def test_ps_blank_headers_print_no_header(opening):
    """opus-3 CALL 137: `ps -o pid=,nlwp=,comm= -u streamroute` printed '    PID NLWP COMMAND'."""
    out, code = sh(opening, "ps -o pid=,nlwp=,comm= -u streamroute")
    assert code == 0 and out.split() == ["1187", "22", "sr-metrics-agent", "2214", "1002", "java"]
    assert sh(opening, "ps -o pid,comm= -u streamroute")[0].startswith("    PID")


def test_the_journal_is_the_hosts_log_group():
    """opus-1 CALL 22: `journalctl -u streamroute -n 40 --no-pager` on sr-a-001 gave two lines; the journal carries
    the same lines FilterLogEvents gives for the host, and the builds and expiry before them."""
    w = scenario_world()
    out = sh(w, "journalctl -u streamroute -n 40 --no-pager", "sr-a-001")[0]
    assert len(out.splitlines()) == 40 and "route.no_shardmap" in out.splitlines()[-1]
    full = sh(w, "journalctl -u streamroute -n 100000", "sr-a-001")[0]
    events = w.serve_aws("logs", "FilterLogEvents", {"logGroupName": "/vl/prod/streamroute",
                                                      "logStreamNames": ["sr-a-001"],
                                                      "startTime": int(w.mech.at("07:00") * 1000)})["events"]
    msgs = [e["message"] for e in events]
    assert any("lease.renew_slow" in m for m in msgs) and any("route.no_shardmap" in m for m in msgs)
    assert all(m in full for m in msgs)
    assert "build epoch=" in full and "complete: 8/8 workers started" in full
    assert "expired (last success" in full and "state BROKEN" in full
    assert full.splitlines()[-1].split()[4] == "streamroute[2214]:"


def test_a_named_terminated_host_is_unreachable_not_dropped():
    """sonnet-1 CALL 44 (08:34:38): 'sr-a-001,sr-a-041,sr-b-001' returned sr-a-001 and sr-b-001 and nothing for
    sr-a-041, which the 08:32 scale-in had terminated."""
    w = world()
    revert(w)
    w.advance_to(at(w, "08:34:38"))
    assert w.mech.fleet.hosts["sr-a-041"].terminated
    out = w.fleet_exec("sr-a-001,sr-a-041,sr-b-001", "hostname", AGENT)
    assert [h["host"] for h in out["hosts"]] == ["sr-a-001", "sr-b-001"]
    assert set(out["unreachable"]) == {"sr-a-041"}
    assert out["unreachable"]["sr-a-041"].startswith("ssh: connect to host sr-a-041 port 22: No route to host")
    assert fleet_outcome(out) == (0, None)
    assert "unreachable" not in w.fleet_exec("sr-a-04*", "hostname", AGENT)
    assert "terminated" in w.fleet_inspect_host("sr-a-041")["error"]
    job = w.fleet_restart("sr-a-041,sr-a-001", 12, AGENT)
    assert job["hosts"] == 1 and job["skipped"]["sr-a-041"].startswith("terminated ")


def test_proc_limits_answer_as_linux_does(opening):
    out, code = sh(opening, "cat /proc/sys/kernel/threads-max /sys/fs/cgroup/pids.max /proc/self/limits",
                   "sr-a-001")  # T03R3-sonnet-1 CALL 28, verbatim
    assert code == 1 and out.startswith("125520\ncat: /sys/fs/cgroup/pids.max: No such file or directory\nLimit ")
    assert "Max processes             62760                62760                processes\n" in out
    assert "Max open files            1024                 524288               files\n" in out
    out, code = sh(opening, "cat /proc/2214/limits")
    assert code == 0 and "Max processes             1024                 1024                 processes\n" in out
    assert "Max open files            65536                65536                files\n" in out
    assert "Max processes             62760" in sh(opening, "cat /proc/1187/limits")[0]
    assert sh(opening, "cat /proc/1/limits")[1] == 0
    assert sh(opening, "cat /proc/2215/limits") == ("cat: /proc/2215/limits: No such file or directory\n", 1)


def test_host_outputs_carry_only_the_design_templates_fields():
    w = world()
    revert(w)
    w.mech.ssm[SRC] = "authoritative"
    assert set(w.fleet_inspect_host("sr-b-017")["shard_map"]) == {"state", "epoch", "last_success", "last_error"}
    status, _ = sh(w, "srctl host status", "sr-b-017")
    assert "last_error" not in status and "rebuild skipped" in status
    w.fleet_restart("sr-a-00*", 12, AGENT)
    w.advance_to(at(w, "08:17:00"))
    fleet, _ = sh(w, "srctl fleet status", "sr-a-001")
    assert "  sr-a-001 WARMING\n" in fleet and "(jvm)" not in fleet and "(bootstrap" not in fleet
    assert sh(w, "systemctl stop streamroute", "sr-b-017") == ("systemctl stop streamroute: refused by srcoord fencing\n", 1)


def test_the_journal_takes_since_and_grep(opening):
    """T03R4-opus-2 CALL 109: `--since 09:20 -g ShardMap` printed the last 50 lines, `route.no_shardmap` among them."""
    w = world()
    w.advance_to(at(w, "08:30:00"))
    since = sh(w, "journalctl -u streamroute --since 08:20 --no-pager")[0].splitlines()
    assert since and all(line[7:15] >= "08:20:00" for line in since)
    assert sh(w, "journalctl -u streamroute --since=08:29:59")[0].count("\n") < len(since)
    case = sh(w, "journalctl -u streamroute -g ShardMap")[0].splitlines()
    assert case and not [line for line in case if "no_shardmap" in line]
    assert [line for line in sh(w, "journalctl -u streamroute -g shardmap")[0].splitlines() if "no_shardmap" in line]
