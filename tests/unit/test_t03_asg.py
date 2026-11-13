"""T03's served Auto Scaling group: refusals, activity history and scheduled-action times as AWS shows them."""

from datetime import UTC, datetime

import pytest
from awsbench_aws.errors import AwsError
from awsbench_aws.world.t03 import PLAT_LAUNCHES
from test_t03_world import asg, at, revert, world


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
