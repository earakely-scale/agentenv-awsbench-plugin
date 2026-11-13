"""The action log: every AWS call, by the agent or the world, and every charged tool call, in issue order."""

import copy
import threading
from datetime import datetime
from typing import Any

FIELDS = ("seq", "t", "t_issue", "call_no", "role", "principal", "world_made", "server", "tool", "service",
          "operation", "params", "error", "resources")


def _iso(t: Any) -> Any:
    return t.isoformat() if isinstance(t, datetime) else t


def _s(value: Any) -> Any:
    """A DynamoDB attribute value's plain value, or the value itself."""
    if isinstance(value, dict) and len(value) == 1:
        return next(iter(value.values()))
    return value


def resources_of(service: str | None, operation: str | None, params: dict | None) -> list[str]:
    """The resources a call names, where they are cheap to read from its params."""
    p, out = params or {}, []
    if service == "route53":
        if zone := p.get("HostedZoneId"):
            out.append(f"route53:zone/{str(zone).rsplit('/', 1)[-1]}")
        for change in (p.get("ChangeBatch") or {}).get("Changes", []) or []:
            rrs = change.get("ResourceRecordSet") or {}
            name = str(rrs.get("Name", "")).rstrip(".")
            ident = f"#{rrs['SetIdentifier']}" if rrs.get("SetIdentifier") else ""
            out.append(f"route53:record/{name}/{rrs.get('Type', '')}{ident}")
    elif service == "dynamodb":
        if table := p.get("TableName"):
            key = p.get("Key") or p.get("Item") or {}
            pk = _s(key.get("pk")) if isinstance(key, dict) and "pk" in key else None
            out.append(f"dynamodb:table/{table}" + (f"/{pk}" if pk is not None else ""))
    elif service == "ssm":
        names = p.get("Names") or ([p["Name"]] if p.get("Name") else []) or ([p["Path"]] if p.get("Path") else [])
        out += [f"ssm:parameter{n}" if str(n).startswith("/") else f"ssm:parameter/{n}" for n in names]
    elif service == "sqs" and p.get("QueueUrl"):
        out.append(f"sqs:queue/{str(p['QueueUrl']).rsplit('/', 1)[-1]}")
    elif service == "ecs":
        if p.get("service"):
            out.append(f"ecs:service/{p.get('cluster', 'default')}/{p['service']}")
    elif service == "application-autoscaling" and p.get("ResourceId"):
        out.append(f"application-autoscaling:{p['ResourceId']}")
    elif service == "logs":
        groups = p.get("logGroupNames") or ([p["logGroupName"]] if p.get("logGroupName") else [])
        out += [f"logs:log-group/{g}" for g in groups]
    elif service == "ec2" and p.get("InstanceIds"):
        out += [f"ec2:instance/{i}" for i in p["InstanceIds"]]
    return out


class ActionLog:
    """Rows as CONTRACTS.md's exports list them; thread-safe, since the bridge records from a worker thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.rows: list[dict] = []

    def record(self, **fields: Any) -> dict:
        row = {k: None for k in FIELDS}
        row.update({k: v for k, v in fields.items() if k in FIELDS})
        row["world_made"] = bool(row["world_made"])
        row["t"], row["t_issue"] = _iso(row["t"]), _iso(row["t_issue"])
        row["params"] = copy.deepcopy(row["params"]) if row["params"] is not None else None
        if row["resources"] is None and row["service"]:
            row["resources"] = resources_of(row["service"], row["operation"], row["params"])
        with self._lock:
            row["seq"] = len(self.rows) + 1
            self.rows.append(row)
        return row

    def export(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.rows]

    def clear(self) -> None:
        with self._lock:
            self.rows.clear()
