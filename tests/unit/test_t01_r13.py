# ruff: noqa: F811
"""T01's world on the R12 and R13 runs' reads: the planner logs every generation (H38, J16); a paused enactor logs no
halt latch, and the host journal stamps the latch at its minute, not at the call (J22); CloudWatch datapoints carry
their metric's unit (J26); ls sorts, shows . and .. with -a and lists the paths it models beside those it doesn't, and
unbound and dig answer as their real tools do (J28, J29); an alarm's reason quotes the datapoints that crossed (H50);
checkouts failing behind the open breaker defer to the outbox (H17); endpointd's CloudTrail rows keep one user name
and principal id (H19)."""

import json
import re

from awsbench_aws.cloudtrail import PLANNER, event
from awsbench_aws.world.endpointd import host_of
from test_world_fleet import run
from test_world_t01 import LEDGER, at, ep
from test_world_telemetry import collapse, messages, stats, world  # noqa: F401

ENACTOR_A = host_of("enactor-a")


def gens(lines):
    return [int(re.search(r"gen=(\d+)", x).group(1)) for x in lines]


def test_the_planner_logs_every_generation(world):
    world.advance_to(at("02:45:00"))
    planner = messages(world, "/vl/platform/endpointd/planner", "02:08:30", "02:31:30")
    assert gens(planner) == list(range(48177, 48200))
    assert [g for g, x in zip(gens(planner), planner) if "changes=0" not in x] == [48180, 48186, 48189]
    early = messages(world, "/vl/platform/endpointd/planner", "01:30:00", "02:08:30")
    assert gens(early) == [48150, 48155, 48171, 48175]


def test_a_paused_enactor_logs_no_halt_latch(world):
    world.advance_to(at("02:45:10"))
    journal = run(world, ENACTOR_A, "journalctl -u endpointd-enactor-a -n 200")[0]
    latch = [x for x in journal.splitlines() if "halt latch engaged" in x]
    assert latch and all(re.search(r" \d\d:\d\d:42 ", x) for x in latch), latch
    assert "02:45:10" not in "".join(latch)
    ep(world, "epctl pause all", "02:49:00")
    world.advance_to(at("02:54:15"))
    stamps = [re.search(r" (\d\d:\d\d:\d\d) ", x).group(1) for x in run(
        world, ENACTOR_A, "journalctl -u endpointd-enactor-a -n 200")[0].splitlines() if "halt latch" in x]
    assert stamps and max(stamps) <= "02:49:42", stamps
    assert not [m for m in messages(world, "/vl/platform/endpointd/enactor-a", "02:49:43", "02:54:15")
                if "halt latch" in m]


def test_datapoints_carry_their_unit(world):
    world.advance_to(at("02:50:00"))
    sqs = stats(world, "AWS/SQS", "ApproximateNumberOfMessagesVisible", "02:40", "02:50", 60, ["Maximum"])
    assert sqs and all(d["Unit"] == "Count" and isinstance(d["Maximum"], float) for d in sqs)
    p99 = stats(world, "Vantleaf/LedgerWriter", "P99LatencyMs", "02:40", "02:50", 60, ["Average"])
    assert p99 and all(d["Unit"] == "Milliseconds" for d in p99)
    assert world.serve_aws("cloudwatch", "GetMetricStatistics", {
        "Namespace": "Vantleaf/LedgerWriter", "MetricName": "P99LatencyMs", "StartTime": at("02:40"),
        "EndTime": at("02:50"), "Period": 60, "Statistics": ["Average"], "Unit": "Seconds"})["Datapoints"] == []


def test_ls_sorts_and_lists_dot_entries_with_a(world):
    out, code = run(world, "li-a-01", "ls -la /etc/envoy/certs/")
    lines = out.splitlines()
    assert code == 0 and lines[0] == "total 16" and lines[1].endswith(" .") and lines[2].endswith(" ..")
    assert [x.split()[-1] for x in lines[3:]] == ["ledger-ingress.key", "ledger-ingress.pem"]


def test_ls_lists_the_paths_it_models_beside_those_it_does_not(world):
    out, code = run(world, "li-a-01", "ls /etc/envoy/certs /var/log")
    assert code == 2 and "'/var/log': not available in this environment" in out and "ledger-ingress.key" in out
    assert run(world, "li-a-01", "ls /var/log")[1] == 127
    out, code = run(world, "li-b-01", "ls -la /etc/envoy/ /etc/envoy/certs /etc/envoy/tls")
    assert code == 2 and "/etc/envoy/:" in out and "/etc/envoy/tls:" in out and "ledger-ingress.pem" in out
    assert "ls: cannot access '/etc/envoy/certs': No such file or directory" in out


def test_unbound_and_dig_answer_as_their_tools_do(world):
    world.advance_to(at("02:50:00"))
    assert run(world, "core-a-01", "unbound-control flush_negative") == (
        "ok removed 1 rrsets, 1 messages and 0 key entries\n", 0)
    assert "ubuntu" not in run(world, "lw-a-01", f"dig {LEDGER}")[0].lower()
    assert "DiG 9.18.28-0ubuntu0.22.04.1-Ubuntu" in run(world, "core-a-01", f"dig {LEDGER}")[0]


def test_an_alarm_reason_quotes_the_datapoints_that_crossed(world):
    def alarm(when):
        world.advance_to(at(when))
        return next(a for a in world.serve_aws("cloudwatch", "DescribeAlarms", {})["MetricAlarms"]
                    if a["AlarmName"] == "webhooks-backlog-high")
    first, later = alarm("02:47:00"), alarm("02:55:00")
    assert first["StateValue"] == later["StateValue"] == "ALARM"
    assert first["StateReason"] == later["StateReason"]
    assert first["StateUpdatedTimestamp"] == later["StateUpdatedTimestamp"]


def test_checkouts_failing_behind_the_open_breaker_defer_to_the_outbox(world):
    collapse(world)
    rows = [m for m in world.telemetry.minutes if at("03:03") <= m["t"] <= at("03:10")]
    assert any(m["regime"] == "collapsed" for m in rows)
    assert rows[-1]["outbox"] - rows[0]["outbox"] > 1000


def test_endpointd_rows_keep_one_user_name_and_principal_id(world):
    world.advance_to(at("02:45:00"))
    principals = {c[2] for c in world.aws.calls if c[1] == "PutItem" and "endpointd" in str(c[2])}
    seeded = event(at("02:31:00"), "dynamodb", "PutItem", {}, PLANNER, session="planner")
    rows = [event(at("02:44:00"), "dynamodb", "PutItem", {}, p) for p in principals] + [seeded]
    planner = [r for r in rows if "planner-role" in r["CloudTrailEvent"]]
    assert len(planner) == 2 and {r["Username"] for r in planner} == {"planner"}
    assert len({json.loads(r["CloudTrailEvent"])["userIdentity"]["principalId"] for r in planner}) == 1
    enactors = [r for r in rows if "enactor-role" in r["CloudTrailEvent"]]
    assert {r["Username"] for r in enactors} <= {"enactor-a", "enactor-b", "enactor-c"}
