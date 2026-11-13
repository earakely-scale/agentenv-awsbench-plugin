"""T03 `vl-streamroute-asg` and its `bf-capacity-hold`, served from the world (sr-asg, sr-hold; docs/T03-build.md
§1.6). The emulator's Auto Scaling is a stub that replaces on wall-clock time, so every operation the agent can call is
answered here, shaped like botocore and refused like AWS. Times are float epoch seconds."""

from datetime import UTC, datetime, timedelta

from ..account import ACCOUNT
from ..errors import AwsError

NAME = "vl-streamroute-asg"
PROCESSES = ["Launch", "Terminate", "AddToLoadBalancer", "AlarmNotification", "AZRebalance", "HealthCheck",
             "InstanceRefresh", "ReplaceUnhealthy", "ScheduledActions"]
HOLD = {"name": "bf-capacity-hold", "recurrence": "0 14 * * 1-5", "time_zone": "America/New_York",
        "start": "2026-11-16T19:00:00Z", "end": "2026-12-02T23:00:00Z", "min": 132, "desired": 132, "max": 150,
        "created": "2026-11-16T06:07:00-05:00"}
PLAT_2291 = [("2026-11-16T06:05:00-05:00", 124, ["sr-a-041", "sr-b-041", "sr-c-041", "sr-a-042"]),
             ("2026-11-16T06:18:00-05:00", 128, ["sr-b-042", "sr-c-042", "sr-a-043", "sr-b-043"]),
             ("2026-11-16T06:31:00-05:00", 132, ["sr-c-043", "sr-a-044", "sr-b-044", "sr-c-044"])]
LT = {"LaunchTemplateId": "lt-07c3e5a9b1d2f4086", "LaunchTemplateName": "vl-streamroute-lt", "Version": "14"}


def _ts(value) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    return datetime.fromisoformat(str(value)).timestamp()


def _cron_field(spec: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        rng, _, step = part.partition("/")
        a, b = (lo, hi) if rng == "*" else (int(rng.split("-")[0]), int(rng.split("-")[-1]))
        out |= set(range(a, b + 1, int(step or 1)))
    return out


def cron_times(expr: str, tz, t0: float, t1: float) -> list[float]:
    """Fire times of a 5-field cron in (t0, t1], evaluated in `tz` (day-of-month and month must be `*`)."""
    mi, hr, _dom, _mon, dow = expr.split()
    mins, hours, days = _cron_field(mi, 0, 59), _cron_field(hr, 0, 23), _cron_field(dow.replace("7", "0"), 0, 6)
    out, day = [], datetime.fromtimestamp(t0, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    while day.timestamp() <= t1:
        if (day.isoweekday() % 7) in days:
            for h in sorted(hours):
                for m in sorted(mins):
                    ts = day.replace(hour=h, minute=m).timestamp()
                    if t0 < ts <= t1:
                        out.append(ts)
        day += timedelta(days=1)
    return out


class Asg:
    def __init__(self, mech, cfg: dict):
        self.m = mech
        a = cfg.get("asg") or {}
        self.min, self.desired, self.max = int(a.get("min", 132)), int(a.get("desired", 132)), int(a.get("max", 150))
        self.reconcile_s = float(a.get("reconcile_s", 60))
        hold = {**HOLD, **(cfg.get("hold") or {})}
        self.actions: dict[str, dict] = {hold["name"]: {**hold, "start": _ts(hold["start"]), "end": _ts(hold["end"]),
                                                        "created": _ts(hold["created"])}}
        self.suspended: dict[str, float] = {}
        self.fired: list[dict] = []
        self.fired_keys: set[tuple[str, float]] = set()
        self.activities: list[dict] = []
        self.timeline: list[list] = []
        self.suspended_timeline: list[list] = []
        self.snapshots: dict[str, list] = {}
        self.next_reconcile: float | None = None
        self.calls: list[dict] = []

    # ---- roster ----
    def instances(self) -> list:
        return [h for h in self.m.fleet.hosts.values() if not h.terminated and not h.detached]

    def inservice(self) -> list:
        return [h for h in self.instances() if not h.standby]

    def seed_history(self) -> None:
        """PLAT-2291's three resizes, one launch activity per instance as AWS writes them; run once the presimulation
        has launched those hosts."""
        for at, size, names in PLAT_2291:
            t = _ts(at)
            for h in (self.m.fleet.hosts[n] for n in names if n in self.m.fleet.hosts):
                self.activity(h.launched, f"Launching a new EC2 instance: {h.iid}",
                              f"At {self._z(t)} a user request update of AutoScalingGroup constraints to min: {size}, "
                              f"max: 150, desired: {size} changing the desired capacity from {size - 4} to {size}.  "
                              f"At {self._z(h.launched)} an instance was started in response to a difference between "
                              f"desired and actual capacity, increasing the capacity from {size - 4} to {size}.")

    def activity(self, t: float, desc: str, cause: str, status: str = "Successful") -> None:
        self.activities.append({"t": t, "n": len(self.activities), "Description": desc, "Cause": cause,
                                "StatusCode": status})

    @staticmethod
    def _z(t: float) -> str:
        return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ---- time ----
    def schedule(self, t0: float, t1: float) -> list[tuple[float, str]]:
        out = []
        for name, a in self.actions.items():
            if a.get("recurrence"):
                times = cron_times(a["recurrence"], self.m.tz, t0, t1)
            else:
                times = [a["start"]] if t0 < a["start"] <= t1 else []
            out += [(ts, name) for ts in times if a["start"] <= ts <= (a.get("end") or ts) and (name, ts) not in
                    self.fired_keys]
        return sorted(out)

    def next_due(self, now: float) -> float | None:
        cands = [x for x in (self.next_reconcile,) if x is not None]
        nxt = self.schedule(now - 1, self.m.t_horizon)
        if nxt:
            cands.append(nxt[0][0])
        return min(cands) if cands else None

    def due(self, t: float) -> None:
        for ts, name in self.schedule(t - 86400, t):
            self.fire(name, ts)
        if self.next_reconcile is not None and t >= self.next_reconcile:
            self.reconcile(t)

    def fire(self, name: str, ts: float) -> None:
        a = self.actions.get(name)
        self.fired_keys.add((name, ts))
        if a is None:
            return
        row = {"t": ts, "name": name, "min": a.get("min"), "desired": a.get("desired"), "max": a.get("max")}
        if "ScheduledActions" in self.suspended:
            self.fired.append({**row, "skipped": "ScheduledActions suspended"})
            return
        new = {k: (a.get(k) if a.get(k) is not None else getattr(self, k)) for k in ("min", "desired", "max")}
        if not new["min"] <= new["desired"] <= new["max"]:
            self.fired.append({**row, "skipped": "invalid size"})
            return
        old = self.desired
        self.min, self.desired, self.max = new["min"], new["desired"], new["max"]
        self.fired.append(row)
        self.m.trail("autoscaling", "UpdateAutoScalingGroup", {"AutoScalingGroupName": NAME, "MinSize": self.min,
                                                               "DesiredCapacity": self.desired, "MaxSize": self.max},
                     ts, "service:autoscaling.amazonaws.com")
        self.activity(ts, f"Scheduled action {name} executed",
                      f"At {self._z(ts)} a scheduled action update of AutoScalingGroup constraints to min: {self.min}, "
                      f"max: {self.max}, desired: {self.desired} changing the desired capacity from {old} to "
                      f"{self.desired}.")
        self.poke(ts)

    def poke(self, t: float) -> None:
        if self.next_reconcile is None:
            self.next_reconcile = t + 1

    def reconcile(self, t: float) -> None:
        self.next_reconcile = None
        live = self.inservice()
        if len(live) > self.desired and "Terminate" not in self.suspended:
            victims = sorted((h for h in live if not h.protected), key=lambda h: (-h.launched, h.name))
            victims = victims[:len(live) - self.desired]
            for h in victims:
                self.m.terminate(h.name, t)
                self.activity(t, f"Terminating EC2 instance: {h.iid}",
                              f"At {self._z(t)} an instance was taken out of service in response to a difference "
                              f"between desired and actual capacity, shrinking the capacity from {len(live)} to "
                              f"{len(live) - len(victims)}.  At {self._z(t)} instance {h.iid} was selected for "
                              "termination.")
        elif len(live) < self.desired and "Launch" not in self.suspended:
            for k in range(self.desired - len(live)):
                h = self.m.fleet.hosts[self.m.launch(t + 5 * k)]
                self.activity(t + 5 * k, f"Launching a new EC2 instance: {h.iid}",
                              f"At {self._z(t)} an instance was started in response to a difference between desired "
                              f"and actual capacity, increasing the capacity from {len(live)} to {self.desired}.")
        if len(self.inservice()) != self.desired:
            self.next_reconcile = t + self.reconcile_s

    def record(self, t: float) -> None:
        self.timeline.append([t, {"min": self.min, "desired": self.desired, "max": self.max,
                                  "inservice": len(self.inservice())}])

    # ---- AWS shapes ----
    def _instance(self, h, now: float) -> dict:
        state = "Standby" if h.standby else "Pending" if now < h.launched + 60 else "InService"
        return {"InstanceId": h.iid, "InstanceType": "c6i.2xlarge", "AvailabilityZone": h.az, "LifecycleState": state,
                "HealthStatus": "Healthy", "LaunchTemplate": LT, "ProtectedFromScaleIn": h.protected}

    def group(self, now: float) -> dict:
        return {
            "AutoScalingGroupName": NAME,
            "AutoScalingGroupARN": f"arn:aws:autoscaling:us-east-1:{ACCOUNT}:autoScalingGroup:9c1e4b7a-2f3d-4a85-b6e0-"
                                   f"7d1c3a5e9f20:autoScalingGroupName/{NAME}",
            "LaunchTemplate": LT, "MinSize": self.min, "MaxSize": self.max, "DesiredCapacity": self.desired,
            "DefaultCooldown": 300, "AvailabilityZones": ["us-east-1a", "us-east-1b", "us-east-1c"],
            "HealthCheckType": "EC2", "HealthCheckGracePeriod": 300,
            "Instances": [self._instance(h, now) for h in sorted(self.instances(), key=lambda h: h.name)],
            "CreatedTime": datetime(2024, 3, 12, 15, 4, 11, tzinfo=UTC),
            "SuspendedProcesses": [{"ProcessName": p, "SuspensionReason": f"User suspended at {self._z(t)}"}
                                   for p, t in sorted(self.suspended.items())],
            "VPCZoneIdentifier": "subnet-0d4a1c7e9b2f36051,subnet-0e5b2d8f1c3a47062,subnet-0f6c3e9a2d4b58073",
            "EnabledMetrics": [], "TerminationPolicies": ["NewestInstance"], "NewInstancesProtectedFromScaleIn": False,
            "ServiceLinkedRoleARN": f"arn:aws:iam::{ACCOUNT}:role/aws-service-role/autoscaling.amazonaws.com/"
                                    "AWSServiceRoleForAutoScaling",
            "Tags": [{"ResourceId": NAME, "ResourceType": "auto-scaling-group", "Key": k, "Value": v,
                      "PropagateAtLaunch": True} for k, v in (("Name", "streamroute"), ("owner", "platform"))],
        }

    def next_start(self, a: dict, now: float) -> float:
        """A recurring action's StartTime as AWS shows it: its next run at or after `now`, else its stored start."""
        if not a.get("recurrence"):
            return a["start"]
        t0 = max(now, a["start"]) - 1
        nxt = cron_times(a["recurrence"], self.m.tz, t0, a.get("end") or t0 + 8 * 86400)
        return nxt[0] if nxt else a["start"]

    def _action(self, name: str, a: dict, now: float) -> dict:
        out = {"AutoScalingGroupName": NAME, "ScheduledActionName": name,
               "ScheduledActionARN": f"arn:aws:autoscaling:us-east-1:{ACCOUNT}:scheduledUpdateGroupAction:"
                                     f"5b8d2e1f-0a7c-4c39-9e64-3f2a1b0c8d7e:autoScalingGroupName/{NAME}:"
                                     f"scheduledActionName/{name}",
               "StartTime": datetime.fromtimestamp(self.next_start(a, now), UTC)}
        if a.get("recurrence"):
            out["Recurrence"] = a["recurrence"]
            out["Time"] = out["StartTime"]
        for key, aws in (("min", "MinSize"), ("max", "MaxSize"), ("desired", "DesiredCapacity")):
            if a.get(key) is not None:
                out[aws] = a[key]
        if a.get("end"):
            out["EndTime"] = datetime.fromtimestamp(a["end"], UTC)
        if a.get("time_zone"):
            out["TimeZone"] = a["time_zone"]
        return out

    def scheduled(self, now: float | None = None) -> list[dict]:
        now = self.m.now if now is None else now
        return [self._action(n, a, now) for n, a in sorted(self.actions.items())]

    # ---- dispatch ----
    def _check_group(self, p: dict) -> None:
        name = p.get("AutoScalingGroupName")
        if name != NAME:
            raise AwsError("ValidationError", f"AutoScalingGroup name not found - AutoScalingGroup {name} not found")

    def _hosts(self, ids: list[str]) -> list:
        by_iid = {h.iid: h for h in self.instances()}
        missing = [i for i in ids if i not in by_iid]
        if missing:
            raise AwsError("ValidationError", f"The instance {missing[0]} is not part of Auto Scaling group {NAME}.")
        return [by_iid[i] for i in ids]

    def _sizes(self, mn: int, des: int, mx: int) -> None:
        if mn > mx:
            raise AwsError("ValidationError", f"Max bound, {mx}, must be greater than or equal to min bound, {mn}")
        if not mn <= des <= mx:
            raise AwsError("ValidationError", f"Desired capacity:{des} must be between the specified min size:{mn} "
                                              f"and max size:{mx}")

    def serve(self, op: str, p: dict, now: float) -> dict | None:
        if op == "DescribeAutoScalingGroups":
            names = p.get("AutoScalingGroupNames")
            return {"AutoScalingGroups": [self.group(now)] if not names or NAME in names else []}
        if op == "DescribeAutoScalingInstances":
            ids = p.get("InstanceIds")
            rows = [{**self._instance(h, now), "AutoScalingGroupName": NAME} for h in sorted(
                self.instances(), key=lambda h: h.name) if not ids or h.iid in ids]
            return {"AutoScalingInstances": rows}
        if op == "DescribeScheduledActions":
            if p.get("AutoScalingGroupName") not in (None, NAME):
                return {"ScheduledUpdateGroupActions": []}
            names = p.get("ScheduledActionNames")
            return {"ScheduledUpdateGroupActions": [a for a in self.scheduled(now)
                                                    if not names or a["ScheduledActionName"] in names]}
        if op == "DescribeScalingActivities":
            if p.get("AutoScalingGroupName") not in (None, NAME):
                return {"Activities": []}
            rows = sorted((a for a in self.activities if a["t"] <= now), key=lambda a: (-a["t"], -a["n"]))
            return {"Activities": [{"ActivityId": f"a1f0{int(a['t']):x}-{a['n']:04x}", "AutoScalingGroupName": NAME,
                                    "Description": a["Description"], "Cause": a["Cause"],
                                    "StartTime": datetime.fromtimestamp(a["t"], UTC),
                                    "EndTime": datetime.fromtimestamp(a["t"] + 30, UTC), "StatusCode": a["StatusCode"],
                                    "Progress": 100, "Details": "{}"} for a in rows[:100]]}
        if op == "DescribeInstanceRefreshes":
            return {"InstanceRefreshes": []}
        if op in WRITES:
            if op != "TerminateInstanceInAutoScalingGroup":  # it names an instance, not its group
                self._check_group(p)
            out = getattr(self, "_" + op)(p, now)
            self.calls.append({"t": now, "op": op, "params": p})
            self.poke(now)
            return out
        return None

    def _UpdateAutoScalingGroup(self, p: dict, now: float) -> dict:
        mn, mx = int(p.get("MinSize", self.min)), int(p.get("MaxSize", self.max))
        des = int(p.get("DesiredCapacity", min(max(self.desired, mn), mx)))
        self._sizes(mn, des, mx)
        self.min, self.desired, self.max = mn, des, mx
        return {}

    def _SetDesiredCapacity(self, p: dict, now: float) -> dict:
        des = int(p["DesiredCapacity"])
        if des < self.min:
            raise AwsError("ValidationError", f"New SetDesiredCapacity value {des} is below min value {self.min} for "
                                              "the AutoScalingGroup.")
        if des > self.max:
            raise AwsError("ValidationError", f"New SetDesiredCapacity value {des} is above max value {self.max} for "
                                              "the AutoScalingGroup.")
        self.desired = des
        return {}

    def _TerminateInstanceInAutoScalingGroup(self, p: dict, now: float) -> dict:
        iid = p["InstanceId"]
        by_iid = {h.iid: h for h in self.instances()}
        if iid not in by_iid:
            raise AwsError("ValidationError", f"Instance Id not found - No managed instance found for instance ID: {iid}")
        dec = bool(p.get("ShouldDecrementDesiredCapacity"))
        if dec and self.desired - 1 < self.min:
            raise AwsError("ValidationError", f"Currently, desiredSize equals minSize ({self.min}). Terminating "
                                              "instance without replacement will violate group's min size constraint. "
                                              "Either set shouldDecrementDesiredCapacity flag to false or lower group's "
                                              "min size.")
        h = by_iid[iid]
        if dec:
            self.desired -= 1
        self.m.terminate(h.name, now)
        desc = f"Terminating EC2 instance: {iid}"
        self.activity(now, desc, f"At {self._z(now)} instance {iid} was taken out of service in response to a user "
                                 "request" + (", shrinking the capacity" if dec else "") + ".")
        return {"Activity": {"ActivityId": f"t{int(now):x}", "AutoScalingGroupName": NAME, "Description": desc,
                             "Cause": "user request", "StartTime": datetime.fromtimestamp(now, UTC),
                             "StatusCode": "InProgress", "Progress": 0}}

    def _DeleteScheduledAction(self, p: dict, now: float) -> dict:
        if self.actions.pop(p.get("ScheduledActionName"), None) is None:
            raise AwsError("ValidationError", "Scheduled Update Group Action not found")
        return {}

    def _BatchDeleteScheduledAction(self, p: dict, now: float) -> dict:
        failed = []
        for name in p.get("ScheduledActionNames") or []:
            if self.actions.pop(name, None) is None:
                failed.append({"ScheduledActionName": name, "ErrorCode": "ValidationError",
                               "ErrorMessage": "Scheduled Update Group Action not found"})
        return {"FailedScheduledActions": failed}

    def _put(self, a: dict, now: float) -> None:
        name = a["ScheduledActionName"]
        old = self.actions.get(name, {})
        start = _ts(a["StartTime"]) if a.get("StartTime") else old.get("start", now + 60)
        if a.get("Time") and not a.get("StartTime"):
            start = _ts(a["Time"])
        self.actions[name] = {"name": name, "recurrence": a.get("Recurrence", old.get("recurrence") if not a.get(
            "StartTime") else a.get("Recurrence")), "time_zone": a.get("TimeZone", old.get("time_zone")),
            "start": start, "end": _ts(a["EndTime"]) if a.get("EndTime") else old.get("end"),
            "min": a.get("MinSize", old.get("min")), "desired": a.get("DesiredCapacity", old.get("desired")),
            "max": a.get("MaxSize", old.get("max")), "created": old.get("created", now)}

    def _PutScheduledUpdateGroupAction(self, p: dict, now: float) -> dict:
        self._put(p, now)
        return {}

    def _BatchPutScheduledUpdateGroupAction(self, p: dict, now: float) -> dict:
        for a in p.get("ScheduledUpdateGroupActions") or []:
            self._put(a, now)
        return {"FailedScheduledUpdateGroupActions": []}

    def _SuspendProcesses(self, p: dict, now: float) -> dict:
        for proc in p.get("ScalingProcesses") or PROCESSES:
            self.suspended.setdefault(proc, now)
        self.suspended_timeline.append([now, sorted(self.suspended)])
        return {}

    def _ResumeProcesses(self, p: dict, now: float) -> dict:
        for proc in p.get("ScalingProcesses") or PROCESSES:
            self.suspended.pop(proc, None)
        self.suspended_timeline.append([now, sorted(self.suspended)])
        return {}

    def _standby(self, p: dict, now: float, enter: bool) -> dict:
        hosts = self._hosts(p.get("InstanceIds") or [])
        dec = bool(p.get("ShouldDecrementDesiredCapacity"))
        if enter and dec and self.desired - len(hosts) < self.min:
            raise AwsError("ValidationError", f"AutoScalingGroup {NAME} has min-size={self.min}, max-size={self.max}, "
                                              f"and desired-size={self.desired}. To place into standby {len(hosts)} "
                                              "instances, please update the AutoScalingGroup sizes appropriately.")
        for h in hosts:
            h.standby = enter
        if enter and dec:
            self.desired -= len(hosts)
        if not enter:
            self.desired = min(self.max, self.desired + len(hosts))
        return {"Activities": [{"ActivityId": f"s{int(now):x}{k}", "AutoScalingGroupName": NAME,
                                "Description": f"{'Moving' if enter else 'Removing'} EC2 instance {'to' if enter else 'from'} Standby: {h.iid}",
                                "Cause": "user request", "StartTime": datetime.fromtimestamp(now, UTC),
                                "StatusCode": "InProgress", "Progress": 50} for k, h in enumerate(hosts)]}

    def _EnterStandby(self, p: dict, now: float) -> dict:
        return self._standby(p, now, True)

    def _ExitStandby(self, p: dict, now: float) -> dict:
        return self._standby(p, now, False)

    def _DetachInstances(self, p: dict, now: float) -> dict:
        hosts = self._hosts(p.get("InstanceIds") or [])
        dec = bool(p.get("ShouldDecrementDesiredCapacity"))
        if dec and self.desired - len(hosts) < self.min:
            raise AwsError("ValidationError", f"AutoScalingGroup {NAME} has min-size={self.min}, max-size={self.max}, "
                                              f"and desired-size={self.desired}. To detach {len(hosts)} instances, "
                                              "please update the AutoScalingGroup sizes appropriately.")
        for h in hosts:
            h.detached = True
        if dec:
            self.desired -= len(hosts)
        return {"Activities": []}

    def _SetInstanceProtection(self, p: dict, now: float) -> dict:
        for h in self._hosts(p.get("InstanceIds") or []):
            h.protected = bool(p.get("ProtectedFromScaleIn"))
        return {}

    def _StartInstanceRefresh(self, p: dict, now: float) -> dict:
        raise AwsError("ValidationError", "An Instance Refresh is not supported for this group in this environment.")

    def _CancelInstanceRefresh(self, p: dict, now: float) -> dict:
        raise AwsError("ActiveInstanceRefreshNotFound",
                       f"No in progress or pending Instance Refresh found for Auto Scaling group {NAME}")

    def export(self, tz) -> dict:
        def iso(t):
            return datetime.fromtimestamp(t, tz).isoformat()
        return {"timeline": [[iso(t), v] for t, v in self.timeline],
                "scheduled_action_firings": [{**f, "t": iso(f["t"])} for f in self.fired],
                "scheduled_actions_at": self.snapshots,
                "suspended_processes": [[iso(t), v] for t, v in self.suspended_timeline],
                "calls": [{"t": iso(c["t"]), "op": c["op"]} for c in self.calls]}


WRITES = {"UpdateAutoScalingGroup", "SetDesiredCapacity", "TerminateInstanceInAutoScalingGroup",
          "DeleteScheduledAction", "BatchDeleteScheduledAction", "PutScheduledUpdateGroupAction",
          "BatchPutScheduledUpdateGroupAction", "SuspendProcesses", "ResumeProcesses", "EnterStandby", "ExitStandby",
          "DetachInstances", "SetInstanceProtection", "StartInstanceRefresh", "CancelInstanceRefresh"}
