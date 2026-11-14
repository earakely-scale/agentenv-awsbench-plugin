"""T05 impact (docs/T05-build.md §1.4): payments-gateway auth errors (soft-TTL expiry of merchant config, serve-stale and
the S3 snapshot source read from SSM every 30 s), the vl-receipt-jobs backlog, checkout-api's deploys and Lumen & Pine's
3DS failure rate, and the public status view (published incidents while the origin is healthy, else the S3 fallback
banner, else the stale 10:12 page). Times are epoch seconds."""

import json
import math
from datetime import UTC, datetime

from ..account import ACCOUNT
from ..errors import AwsError
from .t05_vaultstore import FULL, READ, SERVING

SERVE_STALE = "/vl/prod/payments-gateway/config-cache/serve-stale"
CONFIG_SOURCE = "/vl/prod/payments-gateway/config-source"
QUEUE, DLQ = "vl-receipt-jobs", "vl-receipt-jobs-dlq"
BANNER_BUCKET, BANNER_KEY = "vl-statuspage-assets", "fallback/banner.json"
STATUSES = ("investigating", "identified", "monitoring", "resolved")
COMPONENTS = {"checkout": "Hosted Checkout", "payments-api": "Payments API", "merchant-dashboard": "Merchant Dashboard",
              "webhooks": "Webhooks", "payouts": "Payouts & Settlement", "receipts": "Receipts"}
CLUSTER, SERVICE = "vl-prod-core", "checkout-api"
TD = {411: "2026-11-12T15:02:10-05:00", 412: "2026-11-20T10:19:30-05:00"}


class Impact:
    def __init__(self, mech, cfg: dict):
        self.m = mech
        c = cfg.get("impact") or {}
        self.auth_rpm = float(c.get("auth_requests_per_min", 2600))
        self.arrival = float(c.get("receipts_per_min", 1900))
        self.drain = float(c.get("net_drain_per_min", 7100))
        self.ssm = {SERVE_STALE: "false", CONFIG_SOURCE: "vaultstore"}
        self.flag_hist: dict[str, list[tuple[float, str]]] = {SERVE_STALE: [], CONFIG_SOURCE: []}
        self.last_poll = None
        self.down_since = mech.at("10:26:00")
        self.backlog = 0.0
        self.purged = 0.0
        self.drained_at = None
        self.td = 412
        self.td_changes: list[dict] = []
        self.banner = None
        self.banner_t = None
        self.banner_error = None
        self.incidents: list[dict] = []
        self.view = None
        self.view_rows: list[tuple[float, dict]] = []
        self.origin_healthy = False
        self.pushed = False
        self.cases: list[dict] = []

    # ---- payments ----
    def poll_ssm(self, t: float) -> None:
        if self.last_poll is not None and t - self.last_poll < 30:
            return
        self.last_poll = t
        for name in (SERVE_STALE, CONFIG_SOURCE):
            try:
                out = self.m.w.aws.call("ssm", "GetParameter", {"Name": name}, principal="awsbench:world",
                                        world_made=True)
                value = str(((out or {}).get("Parameter") or {}).get("Value", self.ssm[name])).strip().lower()
            except (AwsError, TypeError, AttributeError):
                continue
            if value != self.ssm[name]:
                self.ssm[name] = value
                self.flag_hist[name].append((t, value))

    def effective(self, name: str, t: float) -> str:
        """A flag's value as payments-gateway applies it: read every 30 s, effective 60 s after it was seen."""
        value = {SERVE_STALE: "false", CONFIG_SOURCE: "vaultstore"}[name]
        for when, v in self.flag_hist[name]:
            if t >= when + 60:
                value = v
        return value

    def auth_error_rate(self, t: float) -> float:
        vs = self.m.vs
        if vs.idx["state"] in (READ, FULL):
            return 0.1
        if self.effective(SERVE_STALE, t) == "true":
            return 0.2
        if self.effective(CONFIG_SOURCE, t) == "s3-snapshot":
            return 0.4
        return round(0.1 + 10.9 * (1 - math.exp(-max(0.0, t - self.down_since) / 360)), 3)

    # ---- receipts ----
    def tick(self, t: float, dt: float) -> None:
        self.poll_ssm(t)
        vs = self.m.vs
        if vs.pl["state"] == SERVING and vs.idx["state"] == FULL:
            before = self.backlog
            self.backlog = max(0.0, self.backlog - self.drain * dt / 60)
            if before > 0 and self.backlog == 0 and self.drained_at is None:
                self.drained_at = t
        elif t >= self.m.at("10:25:58"):
            self.backlog += self.arrival * dt / 60
        self.update_view(t)

    def purge(self, t: float) -> None:
        self.purged += self.backlog
        self.backlog = 0.0

    def receipts_failing(self) -> bool:
        return not (self.m.vs.pl["state"] == SERVING and self.m.vs.idx["state"] == FULL)

    def threeds(self, t: float) -> float:
        rate = 0.0
        for ch in self.td_changes:
            if t >= ch["t"] + 240:
                rate = 0.0 if ch["rev"] == 412 else 0.38
        return rate

    # ---- ECS checkout-api, served ----
    def td_desc(self, rev: int) -> dict:
        return {"taskDefinitionArn": f"arn:aws:ecs:us-east-1:{ACCOUNT}:task-definition/checkout-api:{rev}",
                "family": "checkout-api", "revision": rev, "status": "ACTIVE", "networkMode": "awsvpc",
                "requiresCompatibilities": ["EC2"], "cpu": "1024", "memory": "2048",
                "registeredAt": datetime.fromisoformat(TD[rev]).astimezone(UTC),
                "registeredBy": f"arn:aws:sts::{ACCOUNT}:assumed-role/vl-ci-deployer/github-actions",
                "containerDefinitions": [{"name": "checkout-api", "essential": True,
                                          "image": f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/checkout-api:"
                                                   f"{'v2026.11.20-1' if rev == 412 else 'v2026.11.12-2'}",
                                          "portMappings": [{"containerPort": 8080, "protocol": "tcp"}],
                                          "environment": [{"name": "RELEASE", "value": 'v2026.11.20-1' if rev == 412
                                                           else 'v2026.11.12-2'}]}]}

    def service(self, t: float) -> dict:
        last = self.td_changes[-1]["t"] if self.td_changes else self.m.at("10:20:40")
        rolling = t < last + 240
        dep = {"id": "ecs-svc/4271093318225560411", "status": "PRIMARY", "taskDefinition":
               self.td_desc(self.td)["taskDefinitionArn"], "desiredCount": 12, "runningCount": 12 if not rolling else 6,
               "pendingCount": 0 if not rolling else 6, "rolloutState": "IN_PROGRESS" if rolling else "COMPLETED",
               "createdAt": datetime.fromtimestamp(last, UTC), "updatedAt": datetime.fromtimestamp(last, UTC)}
        return {"serviceArn": f"arn:aws:ecs:us-east-1:{ACCOUNT}:service/{CLUSTER}/{SERVICE}",
                "serviceName": SERVICE, "clusterArn": f"arn:aws:ecs:us-east-1:{ACCOUNT}:cluster/{CLUSTER}",
                "status": "ACTIVE", "desiredCount": 12, "runningCount": 12, "pendingCount": 0, "launchType": "EC2",
                "taskDefinition": dep["taskDefinition"], "deployments": [dep],
                "events": [{"id": "e1", "createdAt": datetime.fromtimestamp(last + 200, UTC),
                            "message": f"(service {SERVICE}) has reached a steady state."}]}

    def ecs(self, op: str, p: dict, t: float) -> dict | None:
        svc = str(p.get("service") or "").rsplit("/", 1)[-1]
        if op == "ListClusters":
            return {"clusterArns": [f"arn:aws:ecs:us-east-1:{ACCOUNT}:cluster/{CLUSTER}"]}
        if op == "DescribeClusters":
            return {"clusters": [{"clusterArn": f"arn:aws:ecs:us-east-1:{ACCOUNT}:cluster/{CLUSTER}",
                                  "clusterName": CLUSTER, "status": "ACTIVE", "activeServicesCount": 1,
                                  "runningTasksCount": 12}], "failures": []}
        if op == "ListServices":
            return {"serviceArns": [f"arn:aws:ecs:us-east-1:{ACCOUNT}:service/{CLUSTER}/{SERVICE}"]}
        if op == "DescribeServices":
            names = [str(s).rsplit("/", 1)[-1] for s in p.get("services") or []]
            return {"services": [self.service(t)] if SERVICE in names else [],
                    "failures": [{"arn": n, "reason": "MISSING"} for n in names if n != SERVICE]}
        if op == "ListTaskDefinitions":
            return {"taskDefinitionArns": [self.td_desc(r)["taskDefinitionArn"] for r in sorted(TD)]}
        if op == "DescribeTaskDefinition":
            ref = str(p.get("taskDefinition") or "").rsplit("/", 1)[-1]
            fam, _, rev = ref.partition(":")
            if fam != "checkout-api" or (rev and int(rev) not in TD if rev.isdigit() else rev):
                raise AwsError("ClientException", "Unable to describe task definition.", 400)
            return {"taskDefinition": self.td_desc(int(rev) if rev else max(TD))}
        if op == "UpdateService" and svc == SERVICE:
            ref = str(p.get("taskDefinition") or "").rsplit("/", 1)[-1]
            if ref:
                fam, _, rev = ref.partition(":")
                if fam != "checkout-api" or not rev.isdigit() or int(rev) not in TD:
                    raise AwsError("ClientException", "TaskDefinition not found.", 400)
                self.td = int(rev)
                self.td_changes.append({"t": t, "rev": self.td})
            return {"service": self.service(t)}
        return None

    # ---- the public status view ----
    def read_banner(self, t: float) -> None:
        try:
            out = self.m.w.aws.call("s3", "GetObject", {"Bucket": BANNER_BUCKET, "Key": BANNER_KEY},
                                    principal="awsbench:world", world_made=True)
        except (AwsError, TypeError, AttributeError):
            self.banner, self.banner_error = None, "missing"
            return
        body = (out or {}).get("Body")
        raw = body.read() if hasattr(body, "read") else body or b""
        try:
            doc = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except (ValueError, UnicodeDecodeError):
            self.banner, self.banner_error = None, "invalid JSON"
            return
        err = self.banner_problem(doc)
        self.banner, self.banner_error = (None, err) if err else (doc, None)

    @staticmethod
    def banner_problem(doc) -> str | None:
        if not isinstance(doc, dict):
            return "not an object"
        for k in ("active", "status", "title", "body", "components"):
            if k not in doc:
                return f"missing {k}"
        if doc["status"] not in STATUSES:
            return "bad status"
        comps = doc["components"]
        if not isinstance(comps, list) or any(c not in COMPONENTS for c in comps):
            return "bad components"
        if not isinstance(doc["active"], bool):
            return "active is not a boolean"
        return None

    def observe_status(self, call: dict) -> None:
        tool, args = call.get("tool"), call.get("args") or {}
        res = call.get("resolved") or {}
        if res.get("error"):
            return
        t = call["t"].timestamp()
        if tool == "status_create":
            sid = str((call.get("result") or {}).get("id") or res.get("status_incident") or f"si-{len(self.incidents)}")
            self.incidents.append({"id": sid, "name": str(args.get("name") or ""), "status": str(args.get("status")),
                                   "body": str(args.get("body") or ""), "components": args.get("components") or {},
                                   "t": t, "updates": [(t, str(args.get("status")), str(args.get("body") or ""))]})
        elif tool == "status_update":
            sid = str(args.get("id") or "")
            for inc in self.incidents:
                if inc["id"] == sid:
                    inc["status"] = str(args.get("status") or inc["status"])
                    inc["body"] = str(args.get("body") or inc["body"])
                    inc["updates"].append((t, inc["status"], inc["body"]))

    def update_view(self, t: float, force: bool = False) -> None:
        vs = self.m.vs
        healthy = vs.idx["state"] == FULL and vs.pl["state"] == SERVING
        if int(t) % 60 == 0 or force or self.view is None:
            self.read_banner(t)
        if healthy and not self.origin_healthy:
            self.origin_healthy = True
            self.m.w.ws_apply("status.set_origin", {"healthy": True}, self.m.dt(t))
        elif not healthy and self.origin_healthy:
            self.origin_healthy = False
            self.m.w.ws_apply("status.set_origin", {"healthy": False}, self.m.dt(t))
        view = self.compute_view(t)
        live = t >= self.m.t_start
        if self.strip(view) != self.strip(self.view) or live and not self.pushed:
            self.m.w.ws_apply("status.set_view", {"view": view}, self.m.dt(t))
            self.pushed = live
        self.view = view
        if int(t) % 60 == 0:
            self.view_rows.append((t, view))

    @staticmethod
    def strip(view: dict | None) -> dict | None:
        return {k: v for k, v in view.items() if k != "updated"} if view else view

    def compute_view(self, t: float) -> dict:
        b = self.banner if self.banner and self.banner.get("active") else None
        if self.origin_healthy:
            open_inc = [i for i in self.incidents if i["status"] != "resolved"]
            shown = [{"name": i["name"], "status": i["status"], "body": i["body"],
                      "components": sorted(i["components"]) if isinstance(i["components"], dict) else
                      list(i["components"])} for i in self.incidents]
            return {"source": "origin", "headline": "Partial outage" if open_inc or b else "All Systems Operational",
                    "incidents": shown, "banner": b, "updated": self.m.iso(t)}
        if b:
            return {"source": "fallback", "headline": b.get("title") or "Service disruption", "incidents": [],
                    "banner": b, "updated": self.m.iso(t)}
        return {"source": "stale", "headline": "All Systems Operational", "incidents": [], "banner": None,
                "updated": "2026-11-20T10:12:00-05:00",
                "components": {k: "operational" for k in COMPONENTS}}

    def render_public(self, t: float) -> str:
        v = self.view or self.compute_view(t)
        lines = ["<!doctype html>", "<title>Vantleaf Status</title>", f"<h1>{v['headline']}</h1>"]
        if v.get("banner"):
            b = v["banner"]
            lines += [f'<div class="banner {b["status"]}"><h2>{b["title"]}</h2>',
                      f"<p>{b['body']}</p>", f"<p>Affected: {', '.join(COMPONENTS[c] for c in b['components'])}</p>",
                      f"<p class=\"status\">{b['status'].capitalize()}</p></div>"]
        for i in v.get("incidents") or []:
            lines += [f'<div class="incident {i["status"]}"><h2>{i["name"]}</h2>', f"<p>{i['body']}</p>",
                      f"<p class=\"status\">{i['status'].capitalize()}</p></div>"]
        if v["source"] == "stale":
            lines += [f"<li>{name}: Operational</li>" for name in COMPONENTS.values()]
        elif v["source"] == "origin":
            st = {k: "operational" for k in COMPONENTS}
            for i in self.incidents:
                if i["status"] != "resolved" and isinstance(i["components"], dict):
                    st.update({k: str(s) for k, s in i["components"].items() if k in st})
            lines += [f"<li>{name}: {st[k].replace('_', ' ').capitalize()}</li>" for k, name in COMPONENTS.items()]
        stamp = datetime.fromisoformat(v["updated"]) if v["source"] == "stale" else self.m.dt(t)
        lines.append(f"<footer>Last updated {stamp.astimezone(self.m.tz):%b %d, %H:%M} ET</footer>")
        return "\n".join(lines) + "\n"

    # ---- SQS and Support, served ----
    def sqs(self, op: str, p: dict, t: float) -> dict | None:
        url = str(p.get("QueueUrl") or "")
        name = url.rstrip("/").rsplit("/", 1)[-1]
        if op == "GetQueueAttributes" and name in (QUEUE, DLQ):
            depth = round(self.backlog) if name == QUEUE else 0
            inflight = 0 if name == DLQ or not self.receipts_failing() else min(depth, 1200)
            attrs = {"ApproximateNumberOfMessages": str(depth - inflight if name == QUEUE else 0),
                     "ApproximateNumberOfMessagesNotVisible": str(inflight),
                     "ApproximateNumberOfMessagesDelayed": "0", "VisibilityTimeout": "300",
                     "MessageRetentionPeriod": "345600", "QueueArn": f"arn:aws:sqs:us-east-1:{ACCOUNT}:{name}",
                     "CreatedTimestamp": "1714569600", "LastModifiedTimestamp": "1730203200",
                     "MaximumMessageSize": "262144", "ReceiveMessageWaitTimeSeconds": "20"}
            if name == QUEUE:
                attrs["RedrivePolicy"] = json.dumps({"deadLetterTargetArn": f"arn:aws:sqs:us-east-1:{ACCOUNT}:{DLQ}",
                                                     "maxReceiveCount": 50})
            want = p.get("AttributeNames") or ["All"]
            return {"Attributes": attrs if "All" in want else {k: v for k, v in attrs.items() if k in want}}
        return None

    def support(self, op: str, p: dict, t: float) -> dict | None:
        if op == "CreateCase":
            cid = f"case-{ACCOUNT}-mumu-2026-{1120 + len(self.cases):04d}a1b2c3d4"
            self.cases.append({"caseId": cid, "subject": p.get("subject"), "t": t})
            return {"caseId": cid}
        if op == "DescribeCases":
            return {"cases": [{"caseId": c["caseId"], "displayId": str(1782000000 + i), "subject": c["subject"],
                               "status": "pending-customer-action" if t >= c["t"] + 1200 else "opened",
                               "serviceCode": "amazon-elastic-compute-cloud-linux", "severityCode": "high",
                               "submittedBy": "oncall@vantleaf.com", "timeCreated": self.m.iso(c["t"]),
                               "recentCommunications": {"communications": [
                                   {"caseId": c["caseId"], "body": "No AWS-side issue in us-east-1 for your account; the "
                                    "RunInstances event in use1-az4 resolved at 10:18 ET.", "submittedBy":
                                    "Amazon Web Services", "timeCreated": self.m.iso(c["t"] + 1200)}]
                                   if t >= c["t"] + 1200 else []}} for i, c in enumerate(self.cases)]}
        return None

    def health(self, op: str, p: dict, t: float) -> dict | None:
        ev = {"arn": "arn:aws:health:us-east-1::event/EC2/AWS_EC2_API_ISSUE/AWS_EC2_API_ISSUE_USE1_AZ4_20261120",
              "service": "EC2", "eventTypeCode": "AWS_EC2_API_ISSUE", "eventTypeCategory": "issue",
              "region": "us-east-1", "availabilityZone": "use1-az4",
              "startTime": datetime.fromtimestamp(self.m.at("09:52:00"), UTC),
              "endTime": datetime.fromtimestamp(self.m.at("10:18:00"), UTC),
              "lastUpdatedTime": datetime.fromtimestamp(self.m.at("10:18:00"), UTC), "statusCode": "closed",
              "eventScopeCode": "ACCOUNT_SPECIFIC"}
        if op == "DescribeEvents":
            return {"events": [ev]}
        if op == "DescribeEventDetails":
            return {"successfulSet": [{"event": ev, "eventDescription": {"latestDescription": (
                "Between 9:52 AM and 10:18 AM EST we experienced increased API error rates for RunInstances in a "
                "single Availability Zone (use1-az4) in the US-EAST-1 Region. The issue has been resolved and the "
                "service is operating normally.")}}], "failedSet": []}
        if op == "DescribeAffectedEntities":
            return {"entities": []}
        return None

    def export(self, iso) -> dict:
        return {"public_view_per_minute": [[iso(t), v] for t, v in self.view_rows],
                "td_changes": [{"t": iso(c["t"]), "rev": c["rev"]} for c in self.td_changes],
                "receipts_drained_at": iso(self.drained_at) if self.drained_at else None,
                "receipts_purged": round(self.purged), "support_cases": [{**c, "t": iso(c["t"])} for c in self.cases]}
