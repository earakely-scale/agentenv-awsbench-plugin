"""The ledger-ingress Auto Scaling group and its paused Graviton instance refresh, served from the world.

The emulator replaces instances on wall-clock time and ignores checkpoints, so the group the agent reads and the cancel
it may send live here. The refresh resumes at `resumes_at` (after T01's horizon) unless cancelled; the handoff is
graded on knowing that.
"""

import hashlib
from datetime import UTC, datetime, timedelta

from ..account import ACCOUNT
from ..errors import AwsError
from .fleet import instance_id
from .util import iso, parse_time

READS = {"DescribePolicies": "ScalingPolicies", "DescribeLifecycleHooks": "LifecycleHooks",
         "DescribeNotificationConfigurations": "NotificationConfigurations", "DescribeWarmPool": "Instances",
         "DescribeLoadBalancerTargetGroups": "LoadBalancerTargetGroups", "DescribeLoadBalancers": "LoadBalancers",
         "DescribeTrafficSources": "TrafficSources", "DescribeScalingActivities": "Activities"}
TAG_FILTERS = {"auto-scaling-group": "ResourceId", "key": "Key", "value": "Value"}


def asg_arn(name: str) -> str:
    """An ECS-managed group's ARN, the same in DescribeAutoScalingGroups and in its capacity provider."""
    h = hashlib.sha256(name.encode()).hexdigest()
    return (f"arn:aws:autoscaling:us-east-1:{ACCOUNT}:autoScalingGroup:{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-"
            f"{h[20:32]}:autoScalingGroupName/{name}")


def _managed(cfg: dict, lt_id: str, sizes: tuple[int, int, int], instances: list[dict], created: datetime) -> dict:
    """An ECS capacity provider's group, as DescribeAutoScalingGroups shows it."""
    name = cfg["name"]
    return {
        "AutoScalingGroupName": name, "AutoScalingGroupARN": asg_arn(name),
        "LaunchTemplate": {"LaunchTemplateId": lt_id, "LaunchTemplateName": cfg["launch_template"],
                           "Version": "$Latest"},
        "MinSize": sizes[0], "MaxSize": sizes[1], "DesiredCapacity": sizes[2], "DefaultCooldown": 300,
        "AvailabilityZones": ["us-east-1a", "us-east-1b", "us-east-1c"], "HealthCheckType": "EC2",
        "HealthCheckGracePeriod": 300, "Instances": instances, "CreatedTime": created, "SuspendedProcesses": [],
        "VPCZoneIdentifier": "subnet-0a6e1f2b3c4d5e601,subnet-0b7f2a3c4d5e6f702,subnet-0c8a3b4d5e6f7a803",
        "EnabledMetrics": [], "TerminationPolicies": ["Default"], "NewInstancesProtectedFromScaleIn": False,
        "ServiceLinkedRoleARN": f"arn:aws:iam::{ACCOUNT}:role/aws-service-role/autoscaling.amazonaws.com/"
                                "AWSServiceRoleForAutoScaling",
        "Tags": [{"ResourceId": name, "ResourceType": "auto-scaling-group", "Key": "AmazonECSManaged", "Value": "",
                  "PropagateAtLaunch": True},
                 {"ResourceId": name, "ResourceType": "auto-scaling-group", "Key": "ecs-capacity-provider",
                  "Value": cfg["capacity_provider"], "PropagateAtLaunch": True}],
    }


class IngressAsg:
    def __init__(self, world, cfg: dict):
        self.w = world
        self.cfg = cfg
        self.name = cfg["name"]
        start = world.start_t
        r = cfg["refresh"]
        self.refresh_start = parse_time(r["start"], start)
        self.resumes_at = parse_time(r["resumes_at"], start)
        self.cancel_s = float(r.get("cancel_s", 40))
        self.lt_version = str(cfg["launch_template_version"])
        self.previous_lt_version = str(cfg["previous_launch_template_version"])
        self.cancelled_at: datetime | None = None
        self.suspended: dict[str, datetime] = {}
        self.calls: list[dict] = []

    def refresh_status(self, now: datetime) -> str:
        if self.cancelled_at is None:
            return "InProgress"
        return "Cancelled" if now >= self.cancelled_at + timedelta(seconds=self.cancel_s) else "Cancelling"

    # ---- AWS shapes ----
    def _lt(self, version: str) -> dict:
        return {"LaunchTemplateId": "lt-0b7f2c9e4a61d3058", "LaunchTemplateName": self.cfg["launch_template"],
                "Version": version}

    def group(self, now: datetime) -> dict:
        az = self.cfg.get("az_names", {})
        hosts = [h for h in self.w.fleet.ingress() if h.get("state") == "running"]
        return {
            "AutoScalingGroupName": self.name,
            "AutoScalingGroupARN": f"arn:aws:autoscaling:us-east-1:{ACCOUNT}:autoScalingGroup:"
                                   f"5d2e7a1c-3b9f-4e60-8c4d-91a7f0e2b6d3:autoScalingGroupName/{self.name}",
            "LaunchTemplate": self._lt(self.lt_version), "MinSize": self.cfg["min"], "MaxSize": self.cfg["max"],
            "DesiredCapacity": self.cfg["desired"], "DefaultCooldown": 300,
            "AvailabilityZones": sorted({az.get(h["az"], h["az"]) for h in hosts}),
            "HealthCheckType": "EC2", "HealthCheckGracePeriod": 300,
            "Instances": [{"InstanceId": instance_id(h["name"]), "InstanceType": h.get("type", "m6i.large"),
                           "AvailabilityZone": az.get(h["az"], h["az"]), "LifecycleState": "InService",
                           "HealthStatus": "Healthy", "LaunchTemplate": self._lt(
                               self.lt_version if h.get("type", "").startswith("m7g") else self.previous_lt_version),
                           "ProtectedFromScaleIn": False} for h in hosts],
            "CreatedTime": parse_time(self.cfg["created"], self.w.start_t),
            "SuspendedProcesses": [{"ProcessName": p, "SuspensionReason": f"User suspended at {iso(t)}"}
                                   for p, t in sorted(self.suspended.items())],
            "VPCZoneIdentifier": "subnet-0a6e1f2b3c4d5e601,subnet-0b7f2a3c4d5e6f702,subnet-0c8a3b4d5e6f7a803",
            "EnabledMetrics": [], "TerminationPolicies": ["Default"], "NewInstancesProtectedFromScaleIn": False,
            "ServiceLinkedRoleARN": f"arn:aws:iam::{ACCOUNT}:role/aws-service-role/autoscaling.amazonaws.com/"
                                    "AWSServiceRoleForAutoScaling",
            "Tags": [{"ResourceId": self.name, "ResourceType": "auto-scaling-group", "Key": "Name",
                      "Value": "ledger-ingress", "PropagateAtLaunch": True},
                     {"ResourceId": self.name, "ResourceType": "auto-scaling-group", "Key": "owner",
                      "Value": "platform", "PropagateAtLaunch": True}],
        }

    def activities(self, now: datetime) -> list[dict]:
        """The refresh's replacements so far, newest first: each slot's old instance out, then its m7g in."""
        rid, out = self.cfg["refresh"]["id"], []
        for a in self.cfg.get("activities") or []:
            t, slot = parse_time(a["at"], self.w.start_t), a["slot"]
            cause = (f"At {t.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')} an instance was taken out of service in "
                     f"response to an instance refresh.  Instance refresh ID: {rid}.")
            for n, (what, iid) in enumerate((("Terminating", instance_id(slot + "/m6i")),
                                             ("Launching a new", instance_id(slot))), 1):
                at = t + timedelta(seconds=5 * (n - 1))
                out.append({"ActivityId": f"{iid[2:10]}-{rid[:4]}-{n:04x}", "AutoScalingGroupName": self.name,
                            "Description": f"{what} EC2 instance: {iid}", "Cause": cause, "StartTime": at,
                            "EndTime": at + timedelta(seconds=60 if n == 1 else 5), "StatusCode": "Successful",
                            "Progress": 100, "Details": "{}"})
        return sorted((a for a in out if a["StartTime"] <= now), key=lambda a: a["StartTime"], reverse=True)

    def refresh(self, now: datetime) -> dict:
        r, status = self.cfg["refresh"], self.refresh_status(now)
        pct, todo = int(r["percentage_complete"]), int(r["instances_to_update"])
        out = {"InstanceRefreshId": r["id"], "AutoScalingGroupName": self.name, "Status": status,
               "StartTime": self.refresh_start, "PercentageComplete": pct, "InstancesToUpdate": todo,
               "ProgressDetails": {"LivePoolProgress": {"PercentageComplete": pct, "InstancesToUpdate": todo}},
               "Preferences": {"MinHealthyPercentage": int(r["min_healthy_pct"]), "InstanceWarmup": 300, "CheckpointPercentages": [50, 100],
                               "CheckpointDelay": int(r["checkpoint_delay_s"]), "SkipMatching": True,
                               "AutoRollback": False, "ScaleInProtectedInstances": "Ignore",
                               "StandbyInstances": "Ignore"},
               "DesiredConfiguration": {"LaunchTemplate": self._lt(self.lt_version)}}
        if status == "InProgress":
            out["StatusReason"] = ("Waiting for checkpoint delay to elapse (resumes "
                                   f"{self.resumes_at.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')})")
        else:
            out["StatusReason"] = "Cancelled due to user request."
            if status == "Cancelled":
                out["EndTime"] = self.cancelled_at + timedelta(seconds=self.cancel_s)
        return out

    # ---- dispatch ----
    def serve(self, op: str, p: dict, now: datetime) -> dict | None:
        if op == "DescribeScheduledActions":
            return {"ScheduledUpdateGroupActions": []}
        if op == "DescribeInstanceRefreshes":
            if p.get("AutoScalingGroupName") != self.name:
                return {"InstanceRefreshes": []}
            ids = p.get("InstanceRefreshIds")
            return {"InstanceRefreshes": [self.refresh(now)] if not ids or self.cfg["refresh"]["id"] in ids else []}
        if p.get("AutoScalingGroupName") != self.name:
            return None
        if op == "CancelInstanceRefresh":
            if self.cancelled_at is not None:
                raise AwsError("ActiveInstanceRefreshNotFound",
                               f"No in progress or pending Instance Refresh found for Auto Scaling group {self.name}")
            self.cancelled_at = now
            self.calls.append({"t": iso(now), "op": op})
            return {"InstanceRefreshId": self.cfg["refresh"]["id"]}
        if op in ("SuspendProcesses", "ResumeProcesses"):
            procs = p.get("ScalingProcesses") or ["Launch", "Terminate", "AddToLoadBalancer", "AlarmNotification",
                                                  "AZRebalance", "HealthCheck", "InstanceRefresh",
                                                  "ReplaceUnhealthy", "ScheduledActions"]
            for proc in procs:
                if op == "SuspendProcesses":
                    self.suspended.setdefault(proc, now)
                else:
                    self.suspended.pop(proc, None)
            self.calls.append({"t": iso(now), "op": op, "processes": list(procs)})
            return {}
        return None

    def export(self, now: datetime) -> dict:
        return {"name": self.name, "refresh_id": self.cfg["refresh"]["id"], "refresh_status": self.refresh_status(now),
                "cancelled_at": iso(self.cancelled_at), "resumes_at": iso(self.resumes_at),
                "held": self.cancelled_at is not None or "InstanceRefresh" in self.suspended,
                "suspended_processes": {k: iso(v) for k, v in sorted(self.suspended.items())}, "calls": self.calls}


class LedgerWriterAsg:
    """cp-ledger's group: one ledger-writer task per host, so managed scaling launches a host for each task above the
    running hosts, up to MaxSize."""

    def __init__(self, world, cfg: dict):
        self.w, self.cfg, self.name = world, cfg, cfg["name"]

    def group(self, now: datetime) -> dict:
        s = self.w.storm
        base = [h for h in self.w.fleet.hosts.values() if h["fleet"] == "ledger-writer"]
        desired = min(max(self.cfg["min"], s.lw_desired), s.max_tasks)
        names = [h["name"] for h in base] + [f"lw-{'abc'[k % 3]}-{5 + k // 3:02d}" for k in range(desired - len(base))]
        azs = {"a": "us-east-1a", "b": "us-east-1b", "c": "us-east-1c"}
        return _managed(self.cfg, "lt-07c3e9a1f5b2d4086", (self.cfg["min"], s.max_tasks, desired), [
            {"InstanceId": instance_id(n), "InstanceType": self.cfg["instance_type"],
             "AvailabilityZone": azs[n.split("-")[1]], "LifecycleState": "InService" if i < s.lw_hosts else "Pending",
             "HealthStatus": "Healthy", "ProtectedFromScaleIn": False} for i, n in enumerate(names)],
            parse_time(self.cfg["created"], self.w.start_t))

    def serve(self, op: str, p: dict, now: datetime) -> dict | None:
        return None


class FleetAsg:
    """A capacity provider's group whose instances are one fleet's hosts, all in service: cp-core's vl-core-asg holds
    the vl-core hosts that ECS places checkout-api and webhooks-dispatcher on."""

    def __init__(self, world, cfg: dict):
        self.w, self.cfg, self.name = world, cfg, cfg["name"]

    def group(self, now: datetime) -> dict:
        fleet = self.w.fleet
        hosts = [h for h in fleet.hosts.values() if h["fleet"] == self.cfg["fleet"]]
        itype, azs = fleet.cfg[self.cfg["fleet"]].get("instance_type"), fleet.az_names()
        lt_id = "lt-0" + hashlib.sha256(self.cfg["launch_template"].encode()).hexdigest()[:16]
        return _managed(self.cfg, lt_id, (len(hosts),) * 3, [
            {"InstanceId": instance_id(h["name"]), "InstanceType": h.get("type", itype),
             "AvailabilityZone": azs.get(h["az"], h["az"]), "LifecycleState": "InService", "HealthStatus": "Healthy",
             "ProtectedFromScaleIn": False} for h in hosts], parse_time(self.cfg["created"], self.w.start_t))

    def serve(self, op: str, p: dict, now: datetime) -> dict | None:
        return None


def serve_groups(groups: list, op: str, p: dict, now: datetime) -> dict | None:
    """The world's groups, and the static capacity-provider groups the scenario's `ecs.fleet_asgs` names."""
    w = groups[0].w if groups else None
    ecs = ((w.scenario.get("world") or {}).get("ecs") or {}) if w else {}
    groups = groups + [FleetAsg(w, c) for c in ecs.get("fleet_asgs") or []]
    if op == "DescribeAutoScalingGroups":
        names = p.get("AutoScalingGroupNames")
        return {"AutoScalingGroups": [g.group(now) for g in groups if not names or g.name in names]}
    if op == "DescribeAutoScalingInstances":
        ids = p.get("InstanceIds")
        return {"AutoScalingInstances": [{**i, "AutoScalingGroupName": g.name} for g in groups
                                         for i in g.group(now)["Instances"] if not ids or i["InstanceId"] in ids]}
    if op == "DescribeTags":
        return {"Tags": [t for g in groups for t in g.group(now)["Tags"] if all(
            t[TAG_FILTERS[f["Name"]]] in f.get("Values", []) for f in p.get("Filters") or []
            if f.get("Name") in TAG_FILTERS)]}
    named = [g for g in groups if g.name == p.get("AutoScalingGroupName")]
    if op in READS and (named or op == "DescribePolicies" and not p.get("AutoScalingGroupName")):
        acts = [a for g in named if hasattr(g, "activities") for a in g.activities(now)]
        return {READS[op]: acts if op == "DescribeScalingActivities" else []}
    for g in groups:
        if (out := g.serve(op, p, now)) is not None:
            return out
    return None
