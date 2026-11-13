"""T03 host commands as an operator types them: several paths, systemctl properties, ps options, the journal and
fleet_list_hosts' refusals."""

import pytest
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
    assert sh(opening, "ps -L -u streamroute")[1] == 1
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
