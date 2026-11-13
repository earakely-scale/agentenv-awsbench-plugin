"""vl-prod-core, the dispatcher's scalable target, the ledger-writer ASG and vl-ledger-db served from the world, and
the agent's ECS and Application Auto Scaling writes reaching the storm through the ops the meter observes."""

from types import SimpleNamespace

import pytest
from awsbench_aws.actionlog import ActionLog
from awsbench_aws.bridge import Bridge
from awsbench_aws.emulator import Emulator
from awsbench_aws.errors import AwsError
from awsbench_aws.meter import Meter
from awsbench_aws.world import World, replay_seed
from test_aws_bridge import PROFILES
from test_grade_t01 import base_exports, grade, oracle_log
from test_world_t01 import LEDGER, SCENARIO, FakeAws, FakeWs, at, ep, set_concurrency

TARGET = "service/vl-prod-core/webhooks-dispatcher"


@pytest.fixture
def world():
    aws = FakeAws()
    replay_seed(SCENARIO, aws)
    w = World(SCENARIO, "eval-seed-1", aws, FakeWs())
    w.start()
    return w


def aws(w, when, service, operation, params):
    """One agent call as the bridge and meter make it: served (or refused) at `when`, then observed with its op."""
    w.advance_to(at(when))
    try:
        out, error = w.serve_aws(service, operation, params), None
    except AwsError as e:
        out, error = None, e.as_dict()
    w.observe({"t": at(when), "tool": "aws_api", "role": "sre-primary", "server": "aws", "args": {},
               "ops": [{"service": service, "operation": operation, "params": params, "error": error}]})
    return out if error is None else error


def oracle(w):
    ep(w, "epctl pause all", "02:49:00")
    set_concurrency(w, "4", "02:51:45")
    ep(w, f"epctl apply --gen 48199 --endpoint {LEDGER} --manual --reason OPS-4452", "02:52:30")


def test_cluster_services_and_tasks_are_served(world):
    with pytest.raises(AwsError, match="ClusterNotFoundException"):
        world.serve_aws("ecs", "ListServices", {})
    arns = world.serve_aws("ecs", "ListServices", {"cluster": "vl-prod-core"})["serviceArns"]
    assert [a.rsplit("/", 1)[-1] for a in arns] == ["checkout-api", "ledger-writer", "webhooks-dispatcher"]
    svcs = {s["serviceName"]: s for s in world.serve_aws("ecs", "DescribeServices", {
        "cluster": "vl-prod-core", "services": ["checkout-api", "ledger-writer", "webhooks-dispatcher"]})["services"]}
    assert svcs["checkout-api"]["taskDefinition"].endswith("task-definition/checkout-api:412")
    assert (svcs["ledger-writer"]["desiredCount"], svcs["ledger-writer"]["runningCount"]) == (12, 12)
    assert svcs["ledger-writer"]["capacityProviderStrategy"][0]["capacityProvider"] == "cp-ledger"
    assert svcs["webhooks-dispatcher"]["runningCount"] == 40
    env = {e["name"] for e in world.serve_aws("ecs", "DescribeTaskDefinition", {"taskDefinition": "checkout-api:411"})[
        "taskDefinition"]["containerDefinitions"][0]["environment"]}
    assert "OUTBOX_TABLE" not in env and "LEDGER_RETRIES" in env
    acme = world.serve_aws("ecs", "ListTasks", {"cluster": "vl-prod-core", "family": "acme-renewer"})["taskArns"]
    task = world.serve_aws("ecs", "DescribeTasks", {"cluster": "vl-prod-core", "tasks": acme})["tasks"][0]
    assert task["lastStatus"] == "RUNNING" and task["startedBy"] == "events-rule/acme-renewer-nightly"
    tgt = world.serve_aws("application-autoscaling", "DescribeScalableTargets", {"ServiceNamespace": "ecs"})
    assert [(t["ResourceId"], t["MinCapacity"], t["MaxCapacity"]) for t in tgt["ScalableTargets"]] == [(TARGET, 6, 40)]
    groups = {g["AutoScalingGroupName"]: g for g in world.serve_aws("autoscaling", "DescribeAutoScalingGroups", {})[
        "AutoScalingGroups"]}
    lw = groups["vl-ledgerwriter-asg"]
    assert (lw["MaxSize"], lw["DesiredCapacity"], len(lw["Instances"])) == (18, 12, 12) and "vl-ledger-ingress-asg" in groups
    db = world.serve_aws("rds", "DescribeDBClusters", {"DBClusterIdentifier": "vl-ledger-db"})["DBClusters"][0]
    assert db["DBClusterParameterGroup"] == "vl-ledger-pg15"
    params = world.serve_aws("rds", "DescribeDBClusterParameters", {"DBClusterParameterGroupName": "vl-ledger-pg15"})
    assert {"ParameterName": "max_connections", "ParameterValue": "400"}.items() <= params["Parameters"][0].items()


def test_scaling_ledger_writer_past_the_connection_budget_degrades_it(world):
    """TR6: tasks 17-18 exhaust vl-ledger-db's 400 connections, so capacity falls to 1,000 req/s."""
    oracle(world)
    resp = aws(world, "02:53:00", "ecs", "UpdateService",
               {"cluster": "vl-prod-core", "service": "ledger-writer", "desiredCount": 24})
    assert resp["service"]["desiredCount"] == 24 and world.storm.lw_desired == 24 and world.storm.lw_running == 12
    world.advance_to(at("02:56:00"))
    lw = world.serve_aws("autoscaling", "DescribeAutoScalingGroups", {"AutoScalingGroupNames": ["vl-ledgerwriter-asg"]})
    states = [i["LifecycleState"] for i in lw["AutoScalingGroups"][0]["Instances"]]
    assert lw["AutoScalingGroups"][0]["DesiredCapacity"] == 18 and states.count("Pending") == 6
    world.advance_to(at("03:30:00"))
    assert world.storm.lw_running == 18 and world.storm.capacity == 1000
    assert any(r != "healthy" for t, r in world.storm.regime_timeline if t >= "2026-11-17T03:01")


def test_failed_or_misdirected_writes_change_nothing(world):
    err = aws(world, "02:45:00", "ecs", "UpdateService",
              {"cluster": "checkout", "service": "ledger-writer", "desiredCount": 24})
    assert err["Code"] == "ClusterNotFoundException"
    world.observe({"t": at("02:45:30"), "tool": "aws_cli", "role": "sre-primary", "server": "aws", "args": {},
                   "ops": [{"service": "ecs", "operation": "UpdateService", "error": {"Code": "AccessDenied"},
                            "params": {"cluster": "vl-prod-core", "service": "ledger-writer", "desiredCount": 24}}]})
    assert aws(world, "02:46:00", "ecs", "StopTask", {"cluster": "vl-prod-core", "task": "abc"})["Code"] == \
        "InvalidParameterException"
    world.advance_to(at("02:55:00"))
    assert world.storm.lw_desired == 12 and world.storm.lw_pending is None and world.ecs.acme_running()


def test_dispatcher_cut_is_reverted_unless_the_target_is_capped_or_suspended(world):
    """TR7: the autoscaler restores a hand-lowered desired count after two minutes."""
    aws(world, "02:45:00", "ecs", "UpdateService",
        {"cluster": "vl-prod-core", "service": "webhooks-dispatcher", "desiredCount": 6})
    assert world.storm.dispatcher_running() == 6
    world.advance_to(at("02:47:10"))
    assert world.storm.dispatcher_running() == 40
    acts = world.serve_aws("application-autoscaling", "DescribeScalingActivities",
                           {"ServiceNamespace": "ecs", "ResourceId": TARGET})["ScalingActivities"]
    assert acts[0]["Description"] == "Setting desired count to 40." and "ALARM" in acts[0]["Cause"]

    aws(world, "02:48:00", "application-autoscaling", "RegisterScalableTarget", {
        "ServiceNamespace": "ecs", "ResourceId": TARGET, "ScalableDimension": "ecs:service:DesiredCount",
        "SuspendedState": {"DynamicScalingInSuspended": True, "DynamicScalingOutSuspended": True}})
    aws(world, "02:48:45", "ecs", "UpdateService",
        {"cluster": "vl-prod-core", "service": "webhooks-dispatcher", "desiredCount": 6})
    world.advance_to(at("02:55:00"))
    assert world.storm.dispatcher_running() == 6

    aws(world, "02:55:30", "application-autoscaling", "RegisterScalableTarget", {
        "ServiceNamespace": "ecs", "ResourceId": TARGET, "ScalableDimension": "ecs:service:DesiredCount",
        "MinCapacity": 4, "MaxCapacity": 8, "SuspendedState": {"DynamicScalingInSuspended": False,
                                                              "DynamicScalingOutSuspended": False}})
    aws(world, "02:56:15", "ecs", "UpdateService",
        {"cluster": "vl-prod-core", "service": "webhooks-dispatcher", "desiredCount": 40})
    world.advance_to(at("03:05:00"))
    assert world.storm.dispatcher_running() == 8
    tgt = world.serve_aws("application-autoscaling", "DescribeScalableTargets", {"ServiceNamespace": "ecs"})
    assert tgt["ScalableTargets"][0]["MaxCapacity"] == 8


def test_rollback_below_412_strands_the_outbox_until_roll_forward(world):
    """TR5: :411 never reads the PAY-881 outbox, so its pending entries stay put and C7 fails."""
    oracle(world)
    resp = aws(world, "02:55:00", "ecs", "UpdateService",
               {"cluster": "vl-prod-core", "service": "checkout-api", "taskDefinition": "checkout-api:411"})
    assert [(d["status"], d["taskDefinition"].rsplit(":", 1)[-1]) for d in resp["service"]["deployments"]] == [
        ("PRIMARY", "411"), ("ACTIVE", "412")]
    world.advance_to(at("02:57:10"))
    assert world.storm.checkout_rev == 411
    pending = world.storm.outbox
    world.advance_to(at("03:40:00"))
    exp = world.export()["ledger"]
    assert world.storm.outbox == pending > 20000 and exp["outbox_frozen"] and exp["checkout_revision"] == 411
    svc = world.serve_aws("ecs", "DescribeServices", {"cluster": "vl-prod-core", "services": ["checkout-api"]})
    assert svc["services"][0]["taskDefinition"].endswith(":411")

    aws(world, "03:40:00", "ecs", "UpdateService",
        {"cluster": "vl-prod-core", "service": "checkout-api", "taskDefinition": "checkout-api:412"})
    world.advance_to(at("04:30:00"))
    exp = world.export()["ledger"]
    assert exp["outbox_pending"] < 500 and not exp["outbox_frozen"]


def test_oracle_leaves_the_outbox_drained_and_unfrozen(world):
    oracle(world)
    world.advance_to(at("05:10:00"))
    exp = world.export()["ledger"]
    assert exp["outbox_pending"] < 500 and exp["outbox_frozen"] is False and exp["checkout_revision"] == 412


def test_stopping_acme_completes_it_and_cancels_the_scheduled_finish(world):
    arn = world.serve_aws("ecs", "ListTasks", {"cluster": "vl-prod-core", "family": "acme-renewer"})["taskArns"][0]
    aws(world, "02:50:00", "ecs", "StopTask", {"cluster": "vl-prod-core", "task": arn, "reason": "SRE-RB-14"})
    assert not world.ecs.acme_running() and world.world_flags["acme_complete"].startswith("2026-11-17T02:50")
    world.advance_to(at("04:00:00"))
    assert {e["id"]: e["status"] for e in world.timeline.export()}.get("ev-acme-done") == "cancelled"
    task = world.serve_aws("ecs", "DescribeTasks", {"cluster": "vl-prod-core", "tasks": [arn]})["tasks"][0]
    assert task["lastStatus"] == "STOPPED" and task["stopCode"] == "UserInitiated"
    assert world.export()["ecs"]["acme_task_arn"] == arn
    assert world.serve_aws("ecs", "ListTasks", {"cluster": "vl-prod-core", "family": "acme-renewer"})["taskArns"] == []


def test_meter_hands_the_world_only_the_calls_own_ops():
    log = ActionLog()
    log.record(call_no=1, service="ssm", operation="GetParameter", params={})
    log.record(call_no=None, world_made=True, service="route53", operation="ChangeResourceRecordSets", params={})
    log.record(call_no=2, service="ecs", operation="DescribeServices", params={"cluster": "vl-prod-core"})
    log.record(call_no=2, service="ecs", operation="UpdateService", params={"desiredCount": 24},
               error={"Code": "AccessDenied"})
    log.record(call_no=2, tool="aws_cli", params={"command": "aws ecs ..."})
    ops = Meter.ops(SimpleNamespace(actionlog=log), 2)
    assert [(o["operation"], o["error"]) for o in ops] == [("DescribeServices", None),
                                                          ("UpdateService", {"Code": "AccessDenied"})]


def test_aws_cli_update_service_moves_the_storm_end_to_end(world):
    """The bridge serves the call from the world and logs it; the meter's ops then move the physics."""
    log = ActionLog()
    bridge = Bridge(Emulator(port=1), log, profiles=PROFILES, world=lambda: world)
    world.advance_to(at("02:45:00"))
    out = bridge.run_cli("aws ecs update-service --cluster vl-prod-core --service ledger-writer --desired-count 24",
                         role="sre-primary", call={"call_no": 7, "t": at("02:45:00"), "tool": "aws_cli"})
    assert out["exit_code"] == 0, out["stderr"]
    assert world.storm.lw_desired == 12
    world.observe({"t": at("02:45:00"), "tool": "aws_cli", "role": "sre-primary", "server": "aws",
                   "args": {"command": "aws ecs update-service"}, "ops": Meter.ops(SimpleNamespace(actionlog=log), 7)})
    world.advance_to(at("02:51:00"))
    assert world.storm.lw_desired == 24 and world.storm.lw_running == 18


def test_c7_fails_while_the_outbox_is_stranded():
    aws_exp, ws_exp = base_exports(oracle_log(), done="04:30", ledger_ok_at="02:52:30", endpoints_final=41,
                                   checkout_ok_at="03:05", restored_gen=48199)
    aws_exp["world"]["ledger"].update(outbox_frozen=True, checkout_revision=411)
    c7 = {r["id"]: r for r in grade.grade_from_inputs(aws_exp, ws_exp)["rows"]}["C7"]
    assert c7["result"] is False and "checkout-api:411 does not read it" in c7["detail"]
