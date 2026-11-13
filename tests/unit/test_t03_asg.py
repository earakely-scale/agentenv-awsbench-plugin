"""T03's served Auto Scaling group: refusals, activity history and scheduled-action times as AWS shows them."""

from datetime import UTC, datetime

import pytest
from awsbench_aws.errors import AwsError
from awsbench_aws.server import load_scenario_file
from awsbench_aws.world.t03 import PLAT_LAUNCHES
from test_t03_world import ASG, asg, at, revert, world


def terminate(w, host, dec=True):
    iid = w.mech.fleet.hosts[host].iid if host in w.mech.fleet.hosts else host
    return w.serve_aws("autoscaling", "TerminateInstanceInAutoScalingGroup",
                       {"InstanceId": iid, "ShouldDecrementDesiredCapacity": dec})


def activities(w):
    return asg(w, "DescribeScalingActivities")["Activities"]


def test_terminate_instance_needs_no_group_name():
    w = world()
    w.advance_to(at(w, "08:30:00"))
    asg(w, "UpdateAutoScalingGroup", MinSize=120)
    out = terminate(w, "sr-a-041")
    assert out["Activity"]["Description"].endswith(w.mech.fleet.hosts["sr-a-041"].iid)
    assert asg(w, "DescribeAutoScalingGroups")["AutoScalingGroups"][0]["DesiredCapacity"] == 131
    assert w.mech.fleet.hosts["sr-a-041"].terminated


def test_terminate_instance_refuses_like_aws():
    w = world()
    with pytest.raises(AwsError, match="desiredSize equals minSize"):
        terminate(w, "sr-a-041")
    with pytest.raises(AwsError, match="Instance Id not found"):
        terminate(w, "i-0123456789abcdef0")


def test_activities_are_one_per_instance():
    w = world()
    launches = [a for a in activities(w) if a["Description"].startswith("Launching")]
    plat = {w.mech.fleet.hosts[n].iid for n in PLAT_LAUNCHES}
    assert len(launches) == 12
    assert {a["Description"].removeprefix("Launching a new EC2 instance: ") for a in launches} == plat
    assert all("user request update" in a["Cause"] for a in launches)
    assert len({a["ActivityId"] for a in activities(w)}) == len(activities(w))
    revert(w)
    w.advance_to(at(w, "08:20:00"))
    gone = [a for a in activities(w) if a["Description"].startswith("Terminating")]
    assert len(gone) == 12 and {a["Description"].rsplit(" ", 1)[-1] for a in gone} == plat
    assert asg(w, "DescribeAutoScalingGroups")["AutoScalingGroups"][0]["TerminationPolicies"] == ["NewestInstance"]


def hold_start(w):
    (a,) = asg(w, "DescribeScheduledActions", ScheduledActionNames=["bf-capacity-hold"])["ScheduledUpdateGroupActions"]
    assert a["Time"] == a["StartTime"]
    return a["StartTime"]


def test_a_recurring_action_shows_its_next_run():
    w = world()
    assert hold_start(w) == datetime(2026, 11, 16, 19, tzinfo=UTC)
    w.advance_to(at(w, "08:30:00"))
    asg(w, "UpdateAutoScalingGroup", MinSize=120, DesiredCapacity=120)
    w.advance_to(at(w, "14:05:00"))
    assert hold_start(w) == datetime(2026, 11, 17, 19, tzinfo=UTC)
    snaps = w.mech.asg.snapshots
    assert snaps["13:00"][0]["StartTime"] == datetime(2026, 11, 16, 19, tzinfo=UTC)


GROUP_READS = ["DescribePolicies", "DescribeLifecycleHooks", "DescribeTags", "DescribeNotificationConfigurations",
               "DescribeWarmPool", "DescribeLoadBalancerTargetGroups", "DescribeLoadBalancers", "DescribeTrafficSources"]


def test_every_group_read_is_served_for_the_group():
    """opus-3 CALL 138: `aws autoscaling describe-policies --auto-scaling-group-name vl-streamroute-asg` answered
    'AutoScalingGroup vl-streamroute-asg not found' while DescribeAutoScalingGroups listed it."""
    w = world()
    w.advance_to(at(w, "08:20:00"))
    for op in GROUP_READS:
        assert asg(w, op) is not None, op
    assert asg(w, "DescribePolicies") == {"ScalingPolicies": []}
    assert asg(w, "DescribeLifecycleHooks") == {"LifecycleHooks": []}
    assert asg(w, "DescribeWarmPool") == {"Instances": []}
    tags = w.serve_aws("autoscaling", "DescribeTags", {"Filters": [{"Name": "auto-scaling-group", "Values": [ASG]}]})
    assert {t["Key"]: t["Value"] for t in tags["Tags"]}["Name"] == "streamroute"
    assert w.serve_aws("autoscaling", "DescribeTags", {"Filters": [{"Name": "key", "Values": ["owner"]}]})["Tags"][0][
        "Value"] == "platform"
    with pytest.raises(AwsError, match="AutoScalingGroup vl-nope not found"):
        w.serve_aws("autoscaling", "DescribePolicies", {"AutoScalingGroupName": "vl-nope"})


def test_unmodelled_group_writes_are_kept_and_echoed():
    w = world()
    out = asg(w, "PutScalingPolicy", PolicyName="cpu-tt", PolicyType="TargetTrackingScaling",
              TargetTrackingConfiguration={"TargetValue": 50.0})
    assert out["PolicyARN"].endswith("policyName/cpu-tt")
    (pol,) = asg(w, "DescribePolicies")["ScalingPolicies"]
    assert pol["PolicyName"] == "cpu-tt" and pol["AutoScalingGroupName"] == ASG
    asg(w, "PutLifecycleHook", LifecycleHookName="drain", LifecycleTransition="autoscaling:EC2_INSTANCE_TERMINATING")
    assert asg(w, "DescribeLifecycleHooks")["LifecycleHooks"][0]["HeartbeatTimeout"] == 3600
    w.serve_aws("autoscaling", "CreateOrUpdateTags", {"Tags": [{"ResourceId": ASG, "ResourceType": "auto-scaling-group",
                                                                "Key": "incident", "Value": "INC-1"}]})
    assert "incident" in {t["Key"] for t in asg(w, "DescribeAutoScalingGroups")["AutoScalingGroups"][0]["Tags"]}
    asg(w, "AttachLoadBalancerTargetGroups", TargetGroupARNs=["arn:tg/1"])
    assert asg(w, "DescribeLoadBalancerTargetGroups")["LoadBalancerTargetGroups"][0]["LoadBalancerTargetGroupARN"] == \
        "arn:tg/1"
    asg(w, "DeletePolicy", PolicyName="cpu-tt")
    assert asg(w, "DescribePolicies") == {"ScalingPolicies": []}
    assert {c["op"] for c in w.mech.asg.calls} >= {"PutScalingPolicy", "PutLifecycleHook", "CreateOrUpdateTags"}
    assert w.mech.asg.desired == 132


def test_the_trail_history_agrees_with_the_group():
    """CloudTrail's PLAT-2291 resizes are the 'user request update' times DescribeScalingActivities gives, and the
    hold's PutScheduledUpdateGroupAction is when the world says the hold was created."""
    hist = load_scenario_file("t03", {})["trail_history"]
    w = world()
    causes = " ".join(a["Cause"] for a in activities(w))
    for r in hist:
        z = datetime.fromisoformat(r["t"]).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if r["operation"] == "UpdateAutoScalingGroup":
            assert f"At {z} a user request update of AutoScalingGroup constraints to min: {r['params']['MinSize']}," \
                in causes
        elif r["operation"] == "PutScheduledUpdateGroupAction":
            assert datetime.fromisoformat(r["t"]).timestamp() == w.mech.asg.actions["bf-capacity-hold"]["created"]
