"""The vl-prod-core ECS cluster, the dispatcher's scalable target and vl-ledger-db, served from the world.

The emulator knows none of them, and physics owns what they change. A write is validated when it is served and its
response shows the result, but it moves the world only when the meter's observe hands it back without an error
(`apply`), so a refused or failed call changes nothing.
"""

import hashlib
from datetime import datetime, timedelta

from ..account import ACCOUNT
from ..errors import AwsError
from .asg import asg_arn
from .fleet import instance_id
from .util import iso, parse_time

REGION = "us-east-1"
DIMENSION = "ecs:service:DesiredCount"
AZS = ("us-east-1a", "us-east-1b", "us-east-1c")


def _name(value) -> str:
    return str(value or "").rsplit("/", 1)[-1]


def _hex(*parts) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:32]


class Ecs:
    def __init__(self, world, cfg: dict):
        self.w, self.cfg, st = world, cfg, world.start_t
        self.cluster = cfg["cluster"]
        self.defs = {fam: {int(r): {**v, "registered": parse_time(v["registered"], st)} for r, v in spec["revisions"].items()}
                     for fam, spec in {**cfg["services"], **cfg.get("tasks", {})}.items()}
        self.services = {}
        for name, s in cfg["services"].items():
            at = parse_time(s["deployed_at"], st)
            self.services[name] = {"rev": int(s["revision"]), "prev": None, "deployed_at": at, "ready_at": at,
                                   "desired": int(s.get("desired", 0)), "gens": {}, "restarted": {}}
        self.rollout_s = float(cfg.get("rollout_s", 120))
        tgt = cfg["dispatcher_target"]
        self.target_id = f"service/{self.cluster}/{tgt['service']}"
        self.policy: dict | None = dict(tgt["policy"])
        self.suspended = {"DynamicScalingInSuspended": False, "DynamicScalingOutSuspended": False,
                          "ScheduledScalingSuspended": False}
        self.activities = [(parse_time(a["at"], st), a["from"], a["to"], "alarm") for a in tgt["activities"]]
        self.acme = cfg.get("tasks", {}).get("acme-renewer")
        self.acme_stopped_by: str | None = None
        self.stopped: dict[str, dict] = {}
        self.calls: list[dict] = []

    # ---- names ----
    def arn(self, kind: str, name: str) -> str:
        return f"arn:aws:ecs:{REGION}:{ACCOUNT}:{kind}/{name}"

    def td_arn(self, family: str, rev: int) -> str:
        return self.arn("task-definition", f"{family}:{rev}")

    def _cluster(self, p: dict) -> None:
        if _name(p.get("cluster") or "default") != self.cluster:
            raise AwsError("ClusterNotFoundException", "Cluster not found.")

    def _service(self, p: dict, key: str = "service") -> str:
        self._cluster(p)
        name = _name(p.get(key))
        if name not in self.services:
            raise AwsError("ServiceNotFoundException", "Service not found.")
        return name

    def _td(self, value: str) -> tuple[str, int]:
        family, _, rev = _name(value).partition(":")
        revs = self.defs.get(family) or {}
        r = int(rev) if rev.isdigit() else (max(revs) if revs and not rev else None)
        if r not in revs:
            raise AwsError("ClientException", "Unable to describe task definition.")
        return family, r

    # ---- state read from physics ----
    def counts(self, name: str) -> tuple[int, int]:
        s = self.w.storm
        if name == "ledger-writer":
            return s.lw_desired, s.lw_running
        if name == "webhooks-dispatcher":
            return s.dispatcher_desired(), s.dispatcher_running()
        d = self.services[name]["desired"]
        return d, d

    def acme_running(self) -> bool:
        return bool(self.acme) and "acme_complete" not in self.w.world_flags

    def _host(self, name: str, i: int) -> str:
        if name == "ledger-writer":
            base = [h["name"] for h in self.w.fleet.hosts.values() if h["fleet"] == "ledger-writer"]
            return base[i] if i < len(base) else f"lw-{'abc'[(i - len(base)) % 3]}-{5 + (i - len(base)) // 3:02d}"
        core = self.w.core_hosts
        return core[(i * 7 + len(name)) % len(core)]

    def _task(self, name: str, i: int) -> dict:
        s = self.services[name]
        tid = _hex(name, s["rev"], i, s["gens"].get(i, 0))
        host = self._host(name, i)
        started = max(s["ready_at"], s["restarted"].get(i, s["ready_at"]))
        spec = self.cfg["services"][name]
        return self._task_shape(tid, name, s["rev"], f"service:{name}", f"ecs-svc/{_hex(name, s['deployed_at'])[:19]}",
                                spec["capacity_provider"], host, started, spec)

    def _task_shape(self, tid, family, rev, group, started_by, cp, host, started, spec) -> dict:
        az = {"a": AZS[0], "b": AZS[1], "c": AZS[2]}.get(host.split("-")[1][0], AZS[0])
        image = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{family}:{self.defs[family][rev]['image']}"
        return {"taskArn": self.arn("task", f"{self.cluster}/{tid}"), "clusterArn": self.arn("cluster", self.cluster),
                "taskDefinitionArn": self.td_arn(family, rev), "group": group, "startedBy": started_by,
                "lastStatus": "RUNNING", "desiredStatus": "RUNNING", "launchType": "EC2", "capacityProviderName": cp,
                "availabilityZone": az, "connectivity": "CONNECTED", "healthStatus": "HEALTHY",
                "containerInstanceArn": self.arn("container-instance", f"{self.cluster}/{_hex('ci', host)}"),
                "cpu": str(spec.get("cpu", 1024)), "memory": str(spec.get("memory", 2048)),
                "createdAt": started - timedelta(seconds=20), "startedAt": started, "version": 3,
                "tags": [{"key": "host", "value": host}, {"key": "ec2-instance", "value": instance_id(host)}],
                "containers": [{"name": family, "image": image, "lastStatus": "RUNNING", "healthStatus": "HEALTHY",
                                "taskArn": self.arn("task", f"{self.cluster}/{tid}")}]}

    def _acme_task(self) -> dict:
        a = self.acme
        start = parse_time(a["started"], self.w.start_t)
        t = self._task_shape(_hex("acme-renewer", iso(start)), "acme-renewer", int(a["revision"]), "family:acme-renewer",
                             a["started_by"], a["capacity_provider"], self.w.core_hosts[3], start, a)
        if not self.acme_running():
            at = parse_time(self.w.world_flags["acme_complete"], self.w.start_t)
            by_agent = self.acme_stopped_by is not None
            t.update(lastStatus="STOPPED", desiredStatus="STOPPED", stoppedAt=at, stoppingAt=at,
                     stopCode="UserInitiated" if by_agent else "EssentialContainerExited",
                     stoppedReason=f"Task stopped by user: {self.acme_stopped_by}" if by_agent else
                     "Essential container in task exited")
            t["containers"][0].update(lastStatus="STOPPED", exitCode=143 if by_agent else 0)
        return t

    def tasks(self, now: datetime) -> list[dict]:
        out = [self._task(n, i) for n in self.services for i in range(self.counts(n)[1])]
        return out + ([self._acme_task()] if self.acme else [])

    def acme_progress(self, now: datetime) -> int:
        a = self.acme
        t0, t1 = self.w.start_t, parse_time(a["completes_at"], self.w.start_t)
        frac = min(1.0, max(0.0, (now - t0) / (t1 - t0)))
        return int(a["done_at_start"] + (a["total"] - a["done_at_start"]) * frac)

    # ---- shapes ----
    def _deployment(self, name, rev, status, state, created, desired, running) -> dict:
        return {"id": f"ecs-svc/{int(_hex(name, rev, iso(created))[:15], 16)}", "status": status,
                "taskDefinition": self.td_arn(name, rev), "desiredCount": desired, "runningCount": running,
                "pendingCount": max(0, desired - running), "failedTasks": 0, "createdAt": created,
                "updatedAt": created, "launchType": "EC2", "rolloutState": state,
                "rolloutStateReason": "ECS deployment " + ("in progress." if state == "IN_PROGRESS" else "completed.")}

    def service(self, name: str, now: datetime) -> dict:
        s, spec = self.services[name], self.cfg["services"][name]
        desired, running = self.counts(name)
        rolling = now < s["ready_at"] and s["prev"] is not None
        deps = [self._deployment(name, s["rev"], "PRIMARY", "IN_PROGRESS" if rolling else "COMPLETED",
                                 s["deployed_at"], desired, running)]
        if rolling:
            deps.append(self._deployment(name, s["prev"], "ACTIVE", "COMPLETED", s["deployed_at"], 0, 0))
        steady = max(s["ready_at"], s["deployed_at"])
        out = {"serviceArn": self.arn("service", f"{self.cluster}/{name}"), "serviceName": name,
               "clusterArn": self.arn("cluster", self.cluster), "status": "ACTIVE", "desiredCount": desired,
               "runningCount": running, "pendingCount": max(0, desired - running),
               "taskDefinition": self.td_arn(name, s["rev"]), "schedulingStrategy": "REPLICA",
               "capacityProviderStrategy": [{"capacityProvider": spec["capacity_provider"], "weight": 1, "base": 0}],
               "deploymentConfiguration": {"maximumPercent": 200, "minimumHealthyPercent": 100,
                                           "deploymentCircuitBreaker": {"enable": True, "rollback": True}},
               "deployments": deps, "createdAt": parse_time(spec["created"], self.w.start_t),
               "placementConstraints": [{"type": c} for c in spec.get("placement", [])],
               "events": [] if rolling or steady > now else [
                   {"id": _hex(name, iso(steady))[:36], "createdAt": steady,
                    "message": f"(service {name}) has reached a steady state."}]}
        return out

    def task_definition(self, family: str, rev: int) -> dict:
        d = self.defs[family][rev]
        return {"taskDefinitionArn": self.td_arn(family, rev), "family": family, "revision": rev, "status": "ACTIVE",
                "networkMode": "awsvpc", "requiresCompatibilities": ["EC2"], "registeredAt": d["registered"],
                "registeredBy": f"arn:aws:sts::{ACCOUNT}:assumed-role/vl-deploy-pipeline/deploy",
                "containerDefinitions": [{"name": family, "essential": True,
                                          "image": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{family}:{d['image']}",
                                          "environment": [{"name": k, "value": str(v)}
                                                          for k, v in sorted((d.get("env") or {}).items())]}]}

    def target(self) -> dict:
        s = self.w.storm
        return {"ServiceNamespace": "ecs", "ResourceId": self.target_id, "ScalableDimension": DIMENSION,
                "MinCapacity": s.disp_min, "MaxCapacity": s.disp_max, "SuspendedState": dict(self.suspended),
                "RoleARN": f"arn:aws:iam::{ACCOUNT}:role/aws-service-role/ecs.application-autoscaling.amazonaws.com/"
                           "AWSServiceRoleForApplicationAutoScaling_ECSService",
                "CreationTime": parse_time(self.cfg["dispatcher_target"]["created"], self.w.start_t),
                "ScalableTargetARN": f"arn:aws:application-autoscaling:{REGION}:{ACCOUNT}:scalable-target/"
                                     f"0ec5{_hex(self.target_id)[:28]}"}

    # ---- RDS ----
    def _rds(self, op: str, p: dict) -> dict | None:
        db = self.cfg["rds"]
        cid = db["cluster"]
        arn = f"arn:aws:rds:{REGION}:{ACCOUNT}:cluster:{cid}"
        if op == "DescribeDBClusters":
            if p.get("DBClusterIdentifier") and _name(p["DBClusterIdentifier"]).split(":")[-1] != cid:
                raise AwsError("DBClusterNotFoundFault", f"DBCluster {p['DBClusterIdentifier']} not found.", 404)
            return {"DBClusters": [{
                "DBClusterIdentifier": cid, "DBClusterArn": arn, "Engine": "aurora-postgresql",
                "EngineVersion": db["engine_version"], "Status": "available", "MultiAZ": True,
                "DBClusterParameterGroup": db["parameter_group"], "Port": 5432, "AvailabilityZones": list(AZS),
                "Endpoint": f"{cid}.cluster-c8x2kq1vn0lp.{REGION}.rds.amazonaws.com",
                "ReaderEndpoint": f"{cid}.cluster-ro-c8x2kq1vn0lp.{REGION}.rds.amazonaws.com",
                "DBClusterMembers": [{"DBInstanceIdentifier": i["id"], "IsClusterWriter": bool(i["writer"]),
                                      "DBClusterParameterGroupStatus": "in-sync", "PromotionTier": 1}
                                     for i in db["instances"]],
                "ClusterCreateTime": parse_time(db["created"], self.w.start_t), "StorageEncrypted": True,
                "DeletionProtection": True}]}
        if op == "DescribeDBInstances":
            want = _name(p.get("DBInstanceIdentifier")).split(":")[-1]
            rows = [i for i in db["instances"] if not want or i["id"] == want]
            if want and not rows:
                raise AwsError("DBInstanceNotFound", f"DBInstance {want} not found.", 404)
            return {"DBInstances": [{"DBInstanceIdentifier": i["id"], "DBInstanceClass": i["class"],
                                     "Engine": "aurora-postgresql", "EngineVersion": db["engine_version"],
                                     "DBInstanceStatus": "available", "DBClusterIdentifier": cid,
                                     "AvailabilityZone": i["az"], "PubliclyAccessible": False,
                                     "DBInstanceArn": f"arn:aws:rds:{REGION}:{ACCOUNT}:db:{i['id']}",
                                     "DBParameterGroups": [{"DBParameterGroupName": db["parameter_group"],
                                                            "ParameterApplyStatus": "in-sync"}]} for i in rows]}
        if op in ("DescribeDBClusterParameters", "DescribeDBParameters"):
            group = p.get("DBClusterParameterGroupName") or p.get("DBParameterGroupName")
            if group != db["parameter_group"]:
                return None
            return {"Parameters": [{"ParameterName": k, "ParameterValue": str(v), "Source": "user",
                                    "ApplyType": "static", "IsModifiable": True} for k, v in db["parameters"].items()]}
        return None

    # ---- dispatch ----
    def serve(self, service: str, op: str, p: dict, now: datetime) -> dict | None:
        """Reads, and writes validated and shown as they will be once applied; None lets the emulator answer."""
        if service == "rds":
            return self._rds(op, p)
        if service == "application-autoscaling":
            return self._aas(op, p, now)
        if service != "ecs":
            return None
        if op == "ListClusters":
            return {"clusterArns": [self.arn("cluster", self.cluster)]}
        if op == "DescribeClusters":
            names = [_name(c) for c in p.get("clusters") or ["default"]]
            tasks = self.tasks(now)
            return {"clusters": [{"clusterArn": self.arn("cluster", c), "clusterName": c, "status": "ACTIVE",
                                  "runningTasksCount": sum(t["lastStatus"] == "RUNNING" for t in tasks),
                                  "pendingTasksCount": sum(max(0, d - r) for d, r in map(self.counts, self.services)),
                                  "activeServicesCount": len(self.services),
                                  "registeredContainerInstancesCount": len(self.w.core_hosts) + max(
                                      12, self.w.storm.lw_hosts),
                                  "capacityProviders": sorted({s["capacity_provider"]
                                                               for s in self.cfg["services"].values()})}
                                 for c in names if c == self.cluster],
                    "failures": [{"arn": self.arn("cluster", c), "reason": "MISSING"} for c in names if c != self.cluster]}
        if op == "ListServices":
            self._cluster(p)
            return {"serviceArns": [self.arn("service", f"{self.cluster}/{n}") for n in self.services]}
        if op == "DescribeServices":
            self._cluster(p)
            names = [_name(s) for s in p.get("services") or []]
            return {"services": [self.service(n, now) for n in names if n in self.services],
                    "failures": [{"arn": self.arn("service", f"{self.cluster}/{n}"), "reason": "MISSING"}
                                 for n in names if n not in self.services]}
        if op == "ListTasks":
            self._cluster(p)
            want = (p.get("desiredStatus") or "RUNNING").upper()
            svc, fam = _name(p.get("serviceName")), p.get("family")
            if svc and svc not in self.services:
                raise AwsError("ServiceNotFoundException", "Service not found.")
            rows = [t for t in self.tasks(now) + list(self.stopped.values()) if t["desiredStatus"] == want
                    and (not svc or t["group"] == f"service:{svc}") and (not fam or t["group"] == f"family:{fam}"
                                                                          or t["group"] == f"service:{fam}")]
            return {"taskArns": [t["taskArn"] for t in rows][:100]}
        if op == "DescribeTasks":
            self._cluster(p)
            known = {_name(t["taskArn"]): t for t in self.tasks(now) + list(self.stopped.values())}
            ids = [_name(t) for t in p.get("tasks") or []]
            return {"tasks": [known[i] for i in ids if i in known],
                    "failures": [{"arn": self.arn("task", f"{self.cluster}/{i}"), "reason": "MISSING"}
                                 for i in ids if i not in known]}
        if op == "DescribeTaskDefinition":
            family, rev = self._td(p.get("taskDefinition"))
            return {"taskDefinition": self.task_definition(family, rev), "tags": []}
        if op == "ListTaskDefinitions":
            pre = p.get("familyPrefix") or ""
            arns = [self.td_arn(f, r) for f in sorted(self.defs) if f.startswith(pre) for r in sorted(self.defs[f])]
            return {"taskDefinitionArns": arns[::-1] if p.get("sort") == "DESC" else arns}
        if op == "ListTaskDefinitionFamilies":
            return {"families": sorted(f for f in self.defs if f.startswith(p.get("familyPrefix") or ""))}
        if op == "DescribeCapacityProviders":
            return {"capacityProviders": [self._capacity_provider(n) for n in sorted(
                {s["capacity_provider"] for s in self.cfg["services"].values()})]}
        if op == "UpdateService":
            return {"service": self._update_service(p, now, commit=False)}
        if op == "StopTask":
            return {"task": self._stop_task(p, now, commit=False)}
        return None

    def _capacity_provider(self, name: str) -> dict:
        asg = self.cfg["capacity_providers"][name]
        return {"capacityProviderArn": self.arn("capacity-provider", name), "name": name, "status": "ACTIVE",
                "autoScalingGroupProvider": {
                    "autoScalingGroupArn": asg_arn(asg),
                    "managedScaling": {"status": "ENABLED", "targetCapacity": 100, "minimumScalingStepSize": 1,
                                       "maximumScalingStepSize": 10000, "instanceWarmupPeriod": 300},
                    "managedTerminationProtection": "DISABLED"}}

    def _update_service(self, p: dict, now: datetime, commit: bool) -> dict:
        name = self._service(p)
        s = self.services[name]
        n = p.get("desiredCount")
        if n is not None and int(n) < 0:
            raise AwsError("InvalidParameterException", "desiredCount must be at least 0.")
        rev = s["rev"]
        if p.get("taskDefinition"):
            family, rev = self._td(p["taskDefinition"])
            if family != name:
                raise AwsError("InvalidParameterException",
                               f"Task definition family {family} does not match the service's family {name}.")
        redeploy = rev != s["rev"] or bool(p.get("forceNewDeployment"))
        if not commit:
            view = self.service(name, now)
            if n is not None:
                view["desiredCount"] = int(n)
            if redeploy:
                view["taskDefinition"] = self.td_arn(name, rev)
                new = self._deployment(name, rev, "PRIMARY", "IN_PROGRESS", now, view["desiredCount"], 0)
                view["deployments"] = [new] + [{**d, "status": "ACTIVE"} for d in view["deployments"][:1]]
            return view
        if n is not None:
            if name == "ledger-writer":
                self.w.storm.set_ledger_writer_desired(int(n), now)
            elif name == "webhooks-dispatcher":
                self.w.storm.set_dispatcher_desired(int(n), now)
            else:
                s["desired"] = int(n)
        if redeploy:
            s.update(prev=s["rev"], rev=rev, deployed_at=now, ready_at=now + timedelta(seconds=self.rollout_s))
            if name == "checkout-api":
                self.w.storm.set_checkout_revision(rev, s["ready_at"])
                env = self.defs[name][rev].get("env") or {}
                self.w.telemetry.extra_logs.append((s["ready_at"], "/vl/prod/checkout-api", (
                    f"INFO checkout-api {self.defs[name][rev]['image']} (checkout-api:{rev}) started; "
                    + ", ".join(f"{k}={v}" for k, v in sorted(env.items())))))
        return {}

    def _stop_task(self, p: dict, now: datetime, commit: bool) -> dict:
        self._cluster(p)
        tid = _name(p.get("task"))
        acme = self._acme_task() if self.acme else None
        if acme and _name(acme["taskArn"]) == tid and self.acme_running():
            if commit:
                self.acme_stopped_by = str(p.get("reason") or "sre-primary")
                done = self.acme_progress(now)
                self.w.world_effect("acme_complete", {"stopped_by": "agent", "message": (
                    f"WARN run stopped (SIGTERM, StopTask): {done:,}/{self.acme['total']:,} certificates renewed; "
                    "the rest renew on the next nightly run")}, now)
            return {**acme, "desiredStatus": "STOPPED", "lastStatus": "RUNNING"}
        for name, s in self.services.items():
            for k in range(self.counts(name)[1]):
                t = self._task(name, k)
                if _name(t["taskArn"]) != tid:
                    continue
                if commit:
                    self.stopped[tid] = {**t, "lastStatus": "STOPPED", "desiredStatus": "STOPPED", "stoppedAt": now,
                                         "stopCode": "UserInitiated",
                                         "stoppedReason": f"Task stopped by user: {p.get('reason') or 'sre-primary'}"}
                    s["gens"][k] = s["gens"].get(k, 0) + 1
                    s["restarted"][k] = now + timedelta(seconds=30)
                return {**t, "desiredStatus": "STOPPED"}
        raise AwsError("InvalidParameterException", "The referenced task was not found.")

    def _aas(self, op: str, p: dict, now: datetime) -> dict | None:
        if p.get("ServiceNamespace", "ecs") != "ecs":
            return None
        rid = p.get("ResourceId")
        mine = rid in (None, self.target_id)
        if op == "DescribeScalableTargets":
            ids = p.get("ResourceIds")
            return {"ScalableTargets": [self.target()] if not ids or self.target_id in ids else []}
        if op == "DescribeScalingPolicies":
            ok = self.policy and mine and (not p.get("PolicyNames") or self.policy["name"] in p["PolicyNames"])
            return {"ScalingPolicies": [self._policy()] if ok else []}
        if op == "DescribeScalingActivities":
            acts = self.activities + list(self.w.storm.disp_events)
            return {"ScalingActivities": [self._activity(*a) for a in sorted(acts, key=lambda a: a[0], reverse=True)]
                    if mine else []}
        if op in ("RegisterScalableTarget", "DeleteScalingPolicy") and not mine:
            raise AwsError("ValidationException", f"No scalable target registered for service namespace: ecs, "
                                                  f"resource ID: {rid}, scalable dimension: {DIMENSION}")
        if op == "RegisterScalableTarget":
            s = self.w.storm
            lo, hi = int(p.get("MinCapacity", s.disp_min)), int(p.get("MaxCapacity", s.disp_max))
            if lo > hi:
                raise AwsError("ValidationException", "Minimum capacity cannot be greater than maximum capacity")
            return {"ScalableTargetARN": self.target()["ScalableTargetARN"]}
        if op == "DeleteScalingPolicy":
            if not self.policy or p.get("PolicyName") != self.policy["name"]:
                raise AwsError("ObjectNotFoundException", f"No scaling policy found for service namespace: ecs, "
                                                          f"resource ID: {rid}, scalable dimension: {DIMENSION}, "
                                                          f"policy name: {p.get('PolicyName')}")
            return {}
        return None

    def _policy(self) -> dict:
        pol = self.policy
        return {"PolicyName": pol["name"], "ServiceNamespace": "ecs", "ResourceId": self.target_id,
                "ScalableDimension": DIMENSION, "PolicyType": "TargetTrackingScaling",
                "PolicyARN": f"arn:aws:autoscaling:{REGION}:{ACCOUNT}:scalingPolicy:{_hex(pol['name'])[:8]}:resource/"
                             f"ecs/{self.target_id}:policyName/{pol['name']}",
                "TargetTrackingScalingPolicyConfiguration": {
                    "TargetValue": float(pol["target"]), "ScaleOutCooldown": pol["scale_out_cooldown_s"],
                    "ScaleInCooldown": pol["scale_in_cooldown_s"], "DisableScaleIn": False,
                    "CustomizedMetricSpecification": {"MetricName": "BacklogPerTask", "Namespace": "Vantleaf/Webhooks",
                                                      "Statistic": "Average"}},
                "CreationTime": parse_time(self.cfg["dispatcher_target"]["created"], self.w.start_t)}

    def _activity(self, t: datetime, a: int, b: int, kind: str) -> dict:
        cause = (f"monitor alarm TargetTracking-{self.target_id}-AlarmHigh in state ALARM triggered policy "
                 f"{self.cfg['dispatcher_target']['policy']['name']}" if kind == "alarm" else
                 "user request: RegisterScalableTarget bounds")
        return {"ActivityId": _hex("act", iso(t), b)[:36], "ServiceNamespace": "ecs", "ResourceId": self.target_id,
                "ScalableDimension": DIMENSION, "Description": f"Setting desired count to {b}.", "Cause": cause,
                "StartTime": t, "EndTime": t + timedelta(seconds=60), "StatusCode": "Successful",
                "StatusMessage": f"Successfully set desired count to {b}. Change successfully fulfilled by ecs."}

    # ---- writes the meter observed ----
    def apply(self, op: dict, t: datetime) -> list[str]:
        """A successful agent op's effect on the world; returns the effect names (timeline `unless` reads them)."""
        service, name, p = op.get("service"), op.get("operation"), op.get("params") or {}
        try:
            if service == "ecs" and name == "UpdateService":
                self._update_service(p, t, commit=True)
                return ["ecs_update_service"]
            if service == "ecs" and name == "StopTask":
                acme = self.acme_running()
                self._stop_task(p, t, commit=True)
                return ["acme_stopped"] if acme and not self.acme_running() else ["task_stopped"]
            if service == "application-autoscaling" and p.get("ResourceId") == self.target_id:
                return self._apply_aas(name, p, t)
        except AwsError:
            return []
        return []

    def _apply_aas(self, name: str, p: dict, t: datetime) -> list[str]:
        s = self.w.storm
        if name == "RegisterScalableTarget":
            before = s.dispatcher_desired()
            s.disp_min = int(p.get("MinCapacity", s.disp_min))
            s.disp_max = int(p.get("MaxCapacity", s.disp_max))
            for k in self.suspended:
                if k in (p.get("SuspendedState") or {}):
                    self.suspended[k] = bool(p["SuspendedState"][k])
            if s.dispatcher_desired() != before:
                s.disp_events.append((t, before, s.dispatcher_desired(), "user"))
        elif name == "DeleteScalingPolicy":
            self.policy = None
        else:
            return []
        s.set_autoscaled(self.policy is not None and not (self.suspended["DynamicScalingInSuspended"]
                                                          or self.suspended["DynamicScalingOutSuspended"]), t)
        return ["dispatcher_target"]

    def export(self, now: datetime) -> dict:
        return {"services": {n: {"desired": self.counts(n)[0], "running": self.counts(n)[1],
                                 "task_definition": f"{n}:{s['rev']}"} for n, s in self.services.items()},
                "dispatcher_target": {"min": self.w.storm.disp_min, "max": self.w.storm.disp_max,
                                      "suspended": dict(self.suspended), "policy": bool(self.policy)},
                "acme_running": self.acme_running(), "acme_stopped_by": self.acme_stopped_by,
                "acme_task_arn": self._acme_task()["taskArn"] if self.acme else None}
