"""An in-memory stand-in for the emulator calls T04's world makes (DynamoDB tables with a us-west-2 replica, SSM,
EventBridge rules, ECS), so the mechanics run in unit tests without the AWS Emulator."""

import copy
import json
from datetime import datetime

from awsbench_aws.errors import AwsError
from awsbench_aws.world import make_world
from awsbench_aws.world.t04_data import item_plain

SSM = {"/routemap/prod/workflows/scaler/enabled": "true", "/routemap/prod/workflows/rebalancer/enabled": "true",
       "/routemap/prod/workflows/reconciler/enabled": "true", "/routemap/prod/workflows/scaler/scope": "all",
       "/routemap/prod/workflows/rebalancer/scope": "all", "/routemap/prod/api/update_route_enabled": "true",
       "/routemap/prod/api/create_route_enabled": "true", "/routemap/prod/publisher/enabled": "true",
       "/routemap/prod/state_table": "routemap-state"}


class FakeAws:
    def __init__(self):
        self.tables: dict[tuple[str, str], dict] = {}
        self.replicas: set[str] = set()
        self.ssm = dict(SSM)
        self.rules = {n: "ENABLED" for n in ("routemap-scaler-tick", "routemap-rebalancer-tick",
                                             "routemap-reconciler-tick", "routemap-nightly-backup")}
        self.service_td = "arn:aws:ecs:us-east-1:000000000000:task-definition/routemap-api:58"
        self.calls: list = []
        self.actionlog = None
        self.acting = None

    def _t(self, name, region):
        region = region or "us-east-1"
        if (name, region) not in self.tables:
            raise AwsError("ResourceNotFoundException", "Requested resource not found", 400)
        return self.tables[(name, region)]

    def _put(self, name, region, raw):
        item = copy.deepcopy(raw)
        k = (item["merchant_id"]["S"], item["rec"]["S"])
        self._t(name, region)[k] = item
        if name in self.replicas:
            other = "us-west-2" if (region or "us-east-1") == "us-east-1" else "us-east-1"
            self.tables[(name, other)][k] = copy.deepcopy(item)

    def call(self, service, operation, params, *, principal="", world_made=True, region=None, log_call=None):
        self.calls.append((service, operation, world_made))
        p = params or {}
        if service == "dynamodb":
            if operation == "CreateTable":
                key = (p["TableName"], region or "us-east-1")
                if key in self.tables:
                    raise AwsError("ResourceInUseException", "Table already exists", 400)
                self.tables[key] = {}
                return {"TableDescription": {"TableName": p["TableName"], "TableStatus": "CREATING"}}
            if operation == "DescribeTable":
                self._t(p["TableName"], region)
                return {"Table": {"TableName": p["TableName"], "TableStatus": "ACTIVE", "DeletionProtectionEnabled":
                                  True, "Replicas": [{"RegionName": "us-west-2"}] if p["TableName"] in self.replicas
                                  else [], "StreamSpecification": {"StreamEnabled": True},
                                  "BillingModeSummary": {"BillingMode": "PAY_PER_REQUEST"}}}
            if operation == "UpdateTable":
                for u in p.get("ReplicaUpdates", []):
                    self.replicas.add(p["TableName"])
                    self.tables[(p["TableName"], u["Create"]["RegionName"])] = copy.deepcopy(
                        self._t(p["TableName"], region))
                return {}
            if operation == "BatchWriteItem":
                for name, reqs in p["RequestItems"].items():
                    for r in reqs:
                        if "PutRequest" in r:
                            self._put(name, region, r["PutRequest"]["Item"])
                return {"UnprocessedItems": {}}
            if operation == "PutItem":
                self._put(p["TableName"], region, p["Item"])
                return {}
            if operation == "DeleteItem":
                k = (p["Key"]["merchant_id"]["S"], p["Key"]["rec"]["S"])
                self._t(p["TableName"], region).pop(k, None)
                if p["TableName"] in self.replicas:
                    self.tables[(p["TableName"], "us-west-2")].pop(k, None)
                return {}
            if operation == "Scan":
                items = list(self._t(p["TableName"], region).values())
                start = int((p.get("ExclusiveStartKey") or {}).get("i", {}).get("N", 0))
                page = items[start:start + 2000]
                out = {"Items": copy.deepcopy(page)}
                if start + 2000 < len(items):
                    out["LastEvaluatedKey"] = {"i": {"N": str(start + 2000)}}
                return out
            if operation == "ListTables":
                return {"TableNames": sorted(n for n, r in self.tables if r == (region or "us-east-1"))}
        if service == "ssm":
            if operation == "GetParametersByPath":
                return {"Parameters": [{"Name": k, "Value": v} for k, v in sorted(self.ssm.items())
                                       if k.startswith(p["Path"])]}
            if operation == "PutParameter":
                self.ssm[p["Name"]] = p["Value"]
                return {"Version": 2}
        if service == "events":
            if operation == "DescribeRule":
                return {"Name": p["Name"], "State": self.rules.get(p["Name"], "ENABLED")}
            if operation in ("DisableRule", "EnableRule"):
                self.rules[p["Name"]] = "DISABLED" if operation == "DisableRule" else "ENABLED"
                return {}
        if service == "ecs":
            if operation == "DescribeServices":
                return {"services": [{"serviceName": "routemap-api", "taskDefinition": self.service_td}]}
            if operation == "UpdateService":
                self.service_td = f"arn:aws:ecs:us-east-1:000000000000:task-definition/{p['taskDefinition']}"
            return {}
        return {}

    def items(self, name="routemap-state", region="us-east-1"):
        return {k: item_plain(v) for k, v in self.tables.get((name, region), {}).items()}


class Ws:
    def __init__(self):
        self.ops = []

    def apply(self, op, args, at):
        self.ops.append((op, args, at))
        return {"ok": True, "id": f"x-{len(self.ops)}", "deliveries": []}


def scenario(timeline=None, npcs=None):
    return {"id": "t04", "timezone": "America/New_York", "start": "2026-11-21T13:12:00-05:00",
            "window_end": "2026-11-21T16:12:00-05:00", "horizon": "2026-11-21T16:12:00-05:00", "tick_s": 10,
            "world": {"mechanics": "t04"}, "timeline": timeline or [], "npcs": npcs or {},
            "aws_seed": [{"service": "logs", "operation": "CreateLogGroup", "params": {"logGroupName": g}}
                         for g in ("/ecs/payments-gateway", "/ecs/routemap-scaler", "/ecs/routemap-rebalancer",
                                   "/ecs/routemap-reconciler", "/ecs/routemap-publisher", "/ecs/routemap-api",
                                   "/ecs/merchant-onboarding", "/routemap/prod/audit")]
            + [{"service": "cloudwatch", "operation": "PutMetricAlarm", "params": {"AlarmName": n}}
               for n in ("payments-route-no-healthy-cell-high", "routemap-api-5xx-high", "cell-7-utilization-high",
                         "cell-7-auth-latency-high", "checkout-cache-evictions")]}


def world(seed="t04-unit"):
    aws = FakeAws()
    w = make_world(scenario(), seed, aws, Ws())
    w.start()
    return w, aws


def at(w, hms):
    h, m, *s = (int(x) for x in hms.split(":"))
    return w.start_t.replace(hour=h, minute=m, second=s[0] if s else 0)


def agent_call(w, tool, t, call_no, ops, args=None):
    """An observed agent call (aws_cli/aws_api) whose AWS operations were `ops`."""
    w.observe({"t": t, "tool": tool, "call_no": call_no, "role": "sre-primary", "args": args or {}, "ops": ops,
               "resolved": {}})


def dumps(x):
    return json.dumps(x, default=lambda o: o.isoformat() if isinstance(o, datetime) else str(o))
