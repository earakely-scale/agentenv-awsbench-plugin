"""CloudTrail LookupEvents, served from the action log in virtual time: every AWS write the agent or the world made,
and the pre-incident writes a scenario's seed replays without times, at the times its items record."""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from .account import ACCOUNT
from .actionlog import ActionLog
from .errors import AwsError
from .meter import is_read
from .world.endpointd import plan_ledger

READ_EXTRA = {"StartQuery", "StopQuery"}
SOURCES = {"cloudwatch": "monitoring"}
PLANNER, ENACTOR = "role:vl-endpointd-planner-role", "role:vl-endpointd-enactor-role"
EXACT = ("EventId", "EventName", "Username", "EventSource", "AccessKeyId")
RESOURCE_KEYS = {"TableName": "AWS::DynamoDB::Table", "HostedZoneId": "AWS::Route53::HostedZone",
                 "AutoScalingGroupName": "AWS::AutoScaling::AutoScalingGroup", "AlarmName": "AWS::CloudWatch::Alarm",
                 "AlarmNames": "AWS::CloudWatch::Alarm", "QueueUrl": "AWS::SQS::Queue",
                 "ResourceId": "AWS::ApplicationAutoScaling::ScalableTarget", "InstanceIds": "AWS::EC2::Instance",
                 "logGroupName": "AWS::Logs::LogGroup"}


def to_time(value: Any) -> datetime:
    """A time as botocore takes one: a datetime, epoch seconds, or ISO 8601 text; naive means UTC."""
    try:
        if isinstance(value, datetime):
            t = value
        elif isinstance(value, (int, float)) or str(value).replace(".", "", 1).isdigit():
            t = datetime.fromtimestamp(float(value), UTC)
        else:
            t = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise AwsError("ValidationException", f"cannot read {value!r} as a time", 400) from None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _plain(value: Any) -> Any:
    return next(iter(value.values())) if isinstance(value, dict) and len(value) == 1 else value


def identity(principal: str | None, session: str | None = None) -> tuple[str, str]:
    """(role, session name) of a principal: `assumed-role/<role>/<name>`, `role:<role>`, `world:<name>` or a name."""
    p = str(principal or "unknown")
    if p.startswith("assumed-role/"):
        _, role, name = (p.split("/", 2) + ["", ""])[:3]
        return role, session or name
    role = p.split(":", 1)[-1]
    return role, session or role


def resources(service: str, params: dict | None) -> list[dict]:
    p, out = params or {}, []

    def add(kind: str, name: Any) -> None:
        r = {"ResourceType": kind, "ResourceName": str(name).rstrip(".").rsplit("/", 1)[-1]
             if kind in ("AWS::Route53::HostedZone", "AWS::SQS::Queue") else str(name).rstrip(".")}
        if name not in (None, "") and r not in out:
            out.append(r)
    for key, kind in RESOURCE_KEYS.items():
        for v in p.get(key) if isinstance(p.get(key), list) else [p.get(key)]:
            add(kind, v)
    if service == "ssm":
        for n in p.get("Names") or [p.get("Name")]:
            add("AWS::SSM::Parameter", n)
    elif service == "route53":
        for change in (p.get("ChangeBatch") or {}).get("Changes") or []:
            add("AWS::Route53::RecordSet", (change.get("ResourceRecordSet") or {}).get("Name"))
    elif service == "dynamodb":
        key = p.get("Key") or p.get("Item") or {}
        add("AWS::DynamoDB::Item", _plain(key.get("pk")) if isinstance(key, dict) else None)
        for table in p.get("RequestItems") or {}:
            add("AWS::DynamoDB::Table", table)
    elif service == "ecs":
        add("AWS::ECS::Service", p.get("service"))
    return out


def event(t: datetime, service: str, operation: str, params: dict | None, principal: str | None, *,
          error: dict | None = None, session: str | None = None, key: str = "", region: str = "us-east-1") -> dict:
    """One LookupEvents event, with its CloudTrailEvent JSON as CloudTrail writes it."""
    eid = str(uuid.UUID(hashlib.sha256(f"{key}|{t.isoformat()}|{service}|{operation}".encode()).hexdigest()[:32]))
    role, user = identity(principal, session)
    source = f"{SOURCES.get(service, service)}.amazonaws.com"
    detail = {"eventVersion": "1.08",
              "userIdentity": {"type": "AssumedRole", "principalId": f"AROA{eid[:13].upper()}:{user}",
                               "arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/{role}/{user}", "accountId": ACCOUNT,
                               "sessionContext": {"sessionIssuer": {"type": "Role", "userName": role,
                                                                    "arn": f"arn:aws:iam::{ACCOUNT}:role/{role}"}}},
              "eventTime": t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "eventSource": source,
              "eventName": operation, "awsRegion": region,
              "requestParameters": {k[:1].lower() + k[1:]: v for k, v in (params or {}).items()},
              "responseElements": None, "eventID": eid, "readOnly": False, "eventType": "AwsApiCall",
              "managementEvent": True, "recipientAccountId": ACCOUNT}
    if error:
        detail.update(errorCode=error.get("Code"), errorMessage=error.get("Message"))
    return {"EventId": eid, "EventName": operation, "ReadOnly": "false", "EventTime": t, "EventSource": source,
            "Username": user, "Resources": resources(service, params),
            "CloudTrailEvent": json.dumps(detail, default=str, separators=(",", ":"))}


def _matches(e: dict, attr: dict) -> bool:
    key, value = attr.get("AttributeKey"), str(attr.get("AttributeValue", ""))
    if key == "ReadOnly":
        return e["ReadOnly"] == value.lower()
    if key in EXACT:
        return e.get(key) == value
    if key in ("ResourceName", "ResourceType"):
        return any(r[key] == value.rstrip(".") for r in e["Resources"])
    raise AwsError("InvalidLookupAttributesException", f"unknown AttributeKey {key!r}; one of {', '.join(EXACT)}, "
                                                       "ResourceName, ResourceType, ReadOnly", 400)


def seed_trail(scenario: dict, rows: list[dict], zone_ids: dict[str, str]) -> list[dict]:
    """The writes before the episode that the seed replays without times. An item recording `applied_at` was written
    then by its `applied_by` enactor, which applied the record change it names first (`applied#<fqdn>`); one recording
    only `created_at` was written then by the planner. A seeded delete is the cleanup one enactor step after the last
    apply, by the enactor that applied the newest generation, which first deletes the records of the plan an
    `applied#` pointer still names, so the trail shows why that name has none."""
    cfg = (scenario.get("world") or {}).get("endpointd") or {}
    step, ttl = float(cfg.get("enactor_step_s", 10)), int((cfg.get("endpoint_table") or {}).get("ttl", 60))
    out, last, newest, plans, pointers = [], None, (-1, None), {}, {}

    def records(fqdn: str, gen: int | None) -> list[dict]:
        """The record sets a plan carries for a name: the ledger's; the other names come from the endpoint table, as
        world.endpointd.Endpointd.plan_records builds them."""
        if fqdn == cfg.get("ledger_fqdn"):
            recs = plan_ledger(plans.get(gen) or {})
        else:
            table = ((cfg.get("endpoint_table") or {}).get("non_ledger") or {}).get(fqdn) or []
            recs = [{"set_id": f"g{gen}-{r['set_id_suffix']}", "ip": r["ip"], "weight": r["weight"]} for r in table]
        return [{"Name": f"{fqdn}.", "Type": "A", "SetIdentifier": r["set_id"], "Weight": r["weight"], "TTL": ttl,
                 "ResourceRecords": [{"Value": r["ip"]}]} for r in recs]

    def dns(t: datetime, by: str, comment: str, action: str, fqdn: str, sets: list[dict]) -> dict:
        zone = next((i for name, i in zone_ids.items() if fqdn.endswith(name)), None)
        change = {**({"HostedZoneId": zone} if zone else {}), "ChangeBatch": {"Comment": comment, "Changes": [
            {"Action": action, "ResourceRecordSet": s} for s in sets]}}
        return event(t, "route53", "ChangeResourceRecordSets", change, ENACTOR, session=by, key=f"seed{len(out)}")

    for row in rows:
        service, op, p = row.get("service"), row.get("operation"), row.get("params") or {}
        if service != "dynamodb":
            continue
        if op == "PutItem":
            item = {k: _plain(v) for k, v in (p.get("Item") or {}).items()}
            when, by = item.get("applied_at") or item.get("created_at"), item.get("applied_by")
            gen, pk = int(item.get("generation") or 0), str(item.get("pk", ""))
            if pk.startswith("plan#"):
                plans[gen] = item
            if not when:
                continue
            t = to_time(when)
            if item.get("applied_at"):
                newest = max(newest, (gen, by), key=lambda g: g[0])
                last = max(last or t, t)
                if pk.startswith("applied#"):
                    fqdn = pk.removeprefix("applied#")
                    pointers[fqdn] = gen
                    if sets := records(fqdn, gen):
                        out.append(dns(t, by, f"endpointd {by} gen={gen}", "UPSERT", fqdn, sets))
            out.append(event(t, service, op, p, ENACTOR if item.get("applied_at") else PLANNER,
                             session=by if item.get("applied_at") else "planner", key=f"seed{len(out)}"))
        elif op == "DeleteItem" and last is not None:
            t, pk = last + timedelta(seconds=step), str(_plain((p.get("Key") or {}).get("pk")) or "")
            gen = int(pk.removeprefix("plan#")) if pk.startswith("plan#") else None
            for fqdn in [f for f, g in pointers.items() if g == gen]:
                if sets := records(fqdn, gen):
                    out.append(dns(t, newest[1], f"endpointd {newest[1]} cleanup gen={gen}", "DELETE", fqdn, sets))
            out.append(event(t, service, op, p, ENACTOR, session=newest[1], key=f"seed{len(out)}"))
    for i, r in enumerate(scenario.get("trail_history") or []):
        out.append(event(to_time(r["t"]), r["service"], r["operation"], r.get("params"), r.get("principal"),
                         key=f"hist{i}"))
    return out


class Trail:
    def __init__(self, actionlog: ActionLog, history: list[dict] | None = None):
        self.actionlog = actionlog
        self.history = list(history or [])

    def events(self) -> list[dict]:
        out = list(self.history)
        for r in self.actionlog.export():
            op = r.get("operation")
            if r.get("service") and op and r.get("t") and not is_read(op) and op not in READ_EXTRA:
                out.append(event(to_time(r["t"]), r["service"], op, r.get("params"), r.get("principal"),
                                 error=r.get("error"), key=f"row{r['seq']}"))
        return out

    def lookup(self, params: dict) -> dict:
        """LookupEvents: newest first, one lookup attribute, StartTime/EndTime, MaxResults (at most 50), NextToken."""
        start = to_time(params["StartTime"]) if params.get("StartTime") is not None else None
        end = to_time(params["EndTime"]) if params.get("EndTime") is not None else None
        if start and end and start > end:
            raise AwsError("InvalidTimeRangeException", "StartTime must be before EndTime", 400)
        attrs = params.get("LookupAttributes") or []
        if len(attrs) > 1:
            raise AwsError("InvalidLookupAttributesException", "You can specify only one lookup attribute", 400)
        hits = [(e["EventTime"], i, e) for i, e in enumerate(self.events())
                if (start is None or e["EventTime"] >= start) and (end is None or e["EventTime"] <= end)
                and all(_matches(e, a) for a in attrs)]
        hits = [e for *_, e in sorted(hits, key=lambda h: (h[0], h[1]), reverse=True)]
        size = max(1, min(int(params.get("MaxResults") or 50), 50))
        token = str(params.get("NextToken") or "0")
        if not token.isdigit():
            raise AwsError("InvalidNextTokenException", "Invalid NextToken", 400)
        offset = int(token)
        out: dict = {"Events": hits[offset:offset + size]}
        if offset + size < len(hits):
            out["NextToken"] = str(offset + size)
        return out
