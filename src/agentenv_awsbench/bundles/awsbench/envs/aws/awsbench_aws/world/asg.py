"""The ledger-ingress Auto Scaling group and its paused Graviton instance refresh, served from the world.

The emulator replaces instances on wall-clock time and ignores checkpoints, so the group the agent reads and the cancel
it may send live here. The refresh resumes at `resumes_at` (after T01's horizon) unless cancelled; the handoff is
graded on knowing that.
"""

from datetime import UTC, datetime, timedelta

from ..errors import AwsError
from .fleet import instance_id
from .util import iso, parse_time

ACCOUNT = "000000000000"


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
            "LaunchTemplate": self._lt("7"), "MinSize": self.cfg["min"], "MaxSize": self.cfg["max"],
            "DesiredCapacity": self.cfg["desired"], "DefaultCooldown": 300,
            "AvailabilityZones": sorted({az.get(h["az"], h["az"]) for h in hosts}),
            "HealthCheckType": "EC2", "HealthCheckGracePeriod": 300,
            "Instances": [{"InstanceId": instance_id(h["name"]), "InstanceType": h.get("type", "m6i.large"),
                           "AvailabilityZone": az.get(h["az"], h["az"]), "LifecycleState": "InService",
                           "HealthStatus": "Healthy", "LaunchTemplate": self._lt("7" if h.get("type", "").startswith(
                               "m7g") else "6"), "ProtectedFromScaleIn": False} for h in hosts],
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

    def refresh(self, now: datetime) -> dict:
        r, status = self.cfg["refresh"], self.refresh_status(now)
        pct, todo = int(r["percentage_complete"]), int(r["instances_to_update"])
        out = {"InstanceRefreshId": r["id"], "AutoScalingGroupName": self.name, "Status": status,
               "StartTime": self.refresh_start, "PercentageComplete": pct, "InstancesToUpdate": todo,
               "ProgressDetails": {"LivePoolProgress": {"PercentageComplete": pct, "InstancesToUpdate": todo}},
               "Preferences": {"MinHealthyPercentage": 90, "InstanceWarmup": 300, "CheckpointPercentages": [50, 100],
                               "CheckpointDelay": int(r["checkpoint_delay_s"]), "SkipMatching": True,
                               "AutoRollback": False, "ScaleInProtectedInstances": "Ignore",
                               "StandbyInstances": "Ignore"},
               "DesiredConfiguration": {"LaunchTemplate": self._lt("7")}}
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
        names = p.get("AutoScalingGroupNames")
        if op == "DescribeAutoScalingGroups":
            return {"AutoScalingGroups": [self.group(now)] if not names or self.name in names else []}
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
