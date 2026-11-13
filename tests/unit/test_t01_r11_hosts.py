"""R11 host-realism fixes, replayed with the runs' exact commands: dig's display options and the EST zone (opus-2
CALL 62, E41), each ledger-ingress host's own boot, uptime and journal (opus-5 CALL 43, E31), `ls` of the parent of the
cert directories and an envoy restart charged as a write (opus-3 CALL 104/107, E42), and `plan diff --live-fleet`
naming what it diffs (opus-6 CALL 62, E43)."""

import pytest
from awsbench_aws import meter
from test_world_fleet import run, world  # noqa: F401  (fixture)
from test_world_t01 import LEDGER, SCENARIO, at, ep

OPUS2_DIG = "dig +noall +answer +authority ledger.internal.vantleaf.io"


def restored(w):
    ep(w, "epctl pause all", "02:49:00")
    ep(w, f"epctl apply --gen 48199 --endpoint {LEDGER} --manual --reason x", "02:52:30")


def test_dig_display_options_print_only_the_sections_asked_for(world):  # noqa: F811
    world.advance_to(at("02:45:00"))
    full = run(world, "core-a-01", f"dig {LEDGER}")[0]
    soa = [x for x in full.splitlines() if "\tSOA\t" in x]
    assert run(world, "core-a-01", OPUS2_DIG) == (soa[0] + "\n", 0)
    assert ";; WHEN: Tue Nov 17 02:45:00 EST 2026\n;; MSG SIZE  rcvd: 112\n" in full
    assert full.startswith(f"; <<>> DiG 9.18.28-0ubuntu0.22.04.1-Ubuntu <<>> {LEDGER}\n;; global options: +cmd\n"
                           ";; Got answer:\n") and "\n\n;; QUESTION SECTION:\n" in full
    assert run(world, "core-a-01", f"dig +noall +answer {LEDGER}") == ("", 0)
    assert run(world, "core-a-01", f"dig +nocmd +nostats +noquestion {LEDGER}")[0].startswith(";; Got answer:")
    restored(world)
    world.advance_to(at("03:21:45"))
    for host in ("core-a-01", "core-b-08", "core-c-08"):
        run(world, host, f"unbound-control flush {LEDGER}")
        out, code = run(world, host, OPUS2_DIG)
        assert code == 0 and out.count("\n") == 1 and f"{LEDGER}.\t" in out and "\tIN\tA\t10.20." in out, out
        assert out.strip() == run(world, host, f"dig +noall +answer {LEDGER}")[0].strip()


def test_date_prints_the_zone_by_name(world):  # noqa: F811
    world.advance_to(at("03:21:45"))
    assert run(world, "core-a-01", "date") == ("Tue Nov 17 03:21:45 EST 2026\n", 0)


def test_ingress_hosts_launched_tonight_boot_and_log_as_their_own(world):  # noqa: F811
    world.advance_to(at("03:20:00"))
    assert " up  1:03,  0 users" in run(world, "li-c-01", "uptime")[0]
    assert " up 41 days,  4:51,  0 users" in run(world, "li-a-02", "uptime")[0]
    journal = run(world, "li-c-01", "journalctl -u envoy")[0]
    assert journal.splitlines()[0] == ("Nov 17 02:16:44 li-c-01 envoy[1880]: [info][main] starting main dispatch loop")
    assert "01:42" not in journal and "01:42" not in "".join(world.fleet.inspect("li-c-01", world.now)["recent_log"])
    for host in ("li-a-02", "li-b-02", "li-c-02"):
        assert run(world, host, "journalctl -u envoy") == ("-- No entries --\n", 0), host
        assert "since Mon 2026-10-06 23:28:11 EDT; 41 days ago" in run(world, host, "systemctl status envoy")[0]
    assert "Nov 17 01:42:31 li-a-01 envoy[1880]" in run(world, "li-a-01", "journalctl -u envoy")[0]
    status = run(world, "li-c-01", "systemctl status envoy")[0]
    assert "since Tue 2026-11-17 02:16:42 EST; 1h 3min ago" in status
    assert "since Tue 2026-11-17 02:03:30 EST" in run(world, "li-b-01", "systemctl status envoy")[0]
    assert " 02:16  " in run(world, "li-c-01", "ps aux")[0] and " Oct06  " in run(world, "li-c-02", "ps aux")[0]


def test_ls_of_the_cert_directories_parent_lists_those_the_host_has(world):  # noqa: F811
    assert run(world, "li-a-01", "ls /etc/envoy") == ("certs\ntls\n", 0)
    assert run(world, "li-b-01", "ls /etc/envoy/") == ("tls\n", 0)
    assert run(world, "li-c-02", "ls /etc/envoy") == ("certs\n", 0)
    assert run(world, "li-b-01", "ls -l /etc/envoy") == (
        "total 4\ndrwxr-x--- 2 envoy envoy 4096 Nov 17 02:03 tls\n", 0)
    assert run(world, "li-a-01", "ls /var/log") == (
        "ls: cannot open directory '/var/log': not available in this environment\n", 127)
    out, code = run(world, "li-a-01", "ls -l /etc/envoy/certs/")
    assert code == 0 and out.startswith("total 8\n")


@pytest.mark.parametrize("cmd, cls", [("systemctl restart envoy", "write"),
                                      ("systemctl --no-pager reload envoy", "write"),
                                      ("systemctl restart endpointd-planner", "write"),
                                      ("systemctl stop endpointd-enactor-b --no-block", "write"),
                                      ("systemctl status envoy", "point_read"),
                                      ("systemctl status endpointd-planner", "point_read"),
                                      ("journalctl -u envoy", "point_read")])
def test_a_service_restart_is_charged_as_a_write(cmd, cls):
    assert meter.classify("fleet_exec", {"command": cmd}, SCENARIO["cost_overrides"], hosts=1) == cls
    assert meter.classify("fleet_exec", {"command": cmd}, SCENARIO["cost_overrides"], hosts=3) == "broad"


def test_plan_diff_against_the_live_fleet_names_each_endpoint(world):  # noqa: F811
    out = ep(world, "epctl plan diff 48199 --live-fleet", "02:49:00")
    lines = out["stdout"].splitlines()
    assert out["exit_code"] == 0 and lines[0] == f"plan 48199 vs live fleet: {LEDGER}"
    others = [x for x in lines if x.endswith(" OK") and ": plan 48199 matches the endpoint table (" in x]
    assert len(others) == 40 and not any(x.startswith(LEDGER) for x in others)
    one = ep(world, f"epctl plan diff 48199 --live-fleet --endpoint {LEDGER}", "02:49:30")
    assert one["exit_code"] == 0 and one["stdout"].count("vs live fleet") == 1
    assert one["stdout"].splitlines()[1:] == lines[1:len(one["stdout"].splitlines())]
    assert "matches the endpoint table" not in one["stdout"]
