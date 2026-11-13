"""AWS reads that physics owns: CloudWatch metrics and alarms, CloudWatch Logs, SQS queue depth and the outbox table."""

import hashlib
import math
import re
from bisect import bisect_right
from datetime import UTC, datetime, timedelta

from ..account import ACCOUNT
from ..errors import AwsError
from .util import iso, parse_time

# name, namespace, metric, comparison, threshold, evaluation periods
ALARMS = [
    ("checkout-api-CheckoutSuccessRate-critical", "Vantleaf/Checkout", "CheckoutSuccessRate", "LessThanThreshold",
     0.95, 1),
    ("ledger-writer-p99-high", "Vantleaf/LedgerWriter", "P99LatencyMs", "GreaterThanThreshold", 1000, 3),
    ("webhooks-backlog-high", "AWS/SQS", "ApproximateNumberOfMessagesVisible", "GreaterThanThreshold", 30000, 1),
    ("endpointd-ApplyLagSeconds-high", "Vantleaf/Endpointd", "ApplyLagSeconds", "GreaterThanThreshold", 300, 1),
    ("ledger-ingress-EnvoyNotReady", "Vantleaf/LedgerIngress", "EnvoyNotReady", "GreaterThanThreshold", 0, 1),
]
APP_GROUPS = {"/vl/prod/checkout-api": "checkout", "/vl/prod/webhooks-dispatcher": "dispatcher",
              "/vl/prod/ledger-writer": "ledger"}
OUTBOX = "vl-ledger-outbox"
SUPPORTED = "supported: fields, filter @message like/=~, sort @timestamp, limit, stats count() by bin()/@log"
_FIELDS = ("@timestamp", "@message", "@log", "@logStream")
_TERM = re.compile(r"\s*(?:(and|or)\s+)?@message\s+(not\s+like|like|=~)\s+(/(?:\\.|[^/\\])*/|\"[^\"]*\"|'[^']*')\s*",
                   re.IGNORECASE)
_BIN = re.compile(r"bin\(\s*(\d+)\s*(s|m|h)\s*\)$")
_STATS = re.compile(r"(count\(\s*\*?\s*\))\s*(?:as\s+(\w+))?\s*(?:by\s+(.+))?")


def _utc(t) -> datetime:
    if isinstance(t, (int, float)):
        return datetime.fromtimestamp(t / 1000 if t > 1e11 else t, UTC)
    if isinstance(t, str):
        return datetime.fromisoformat(t.replace("Z", "+00:00"))
    return t.astimezone(UTC)


def _group(g) -> str:
    g = str(g or "")
    return g.split("log-group:", 1)[1].removesuffix(":*") if "log-group:" in g else g


def _stat(vals: list[float], s: str) -> float:
    if s == "Average":
        return round(sum(vals) / len(vals), 4)
    if s == "Sum":
        return round(sum(vals), 4)
    if s == "SampleCount":
        return float(len(vals))
    if s in ("Minimum", "Maximum"):
        return (min if s == "Minimum" else max)(vals)
    return sorted(vals)[max(0, math.ceil(float(s[1:]) / 100 * len(vals)) - 1)]


def _fmt(v: float) -> str:
    return f"{v:.4f}".rstrip("0").rstrip(".")


def _split(qs: str) -> list[str]:
    """Top-level `|` commands, keeping pipes inside /regex/ and quotes."""
    out, buf, quote, i = [], "", None, 0
    while i < len(qs):
        ch = qs[i]
        if quote and ch == "\\":
            buf, i = buf + qs[i:i + 2], i + 2
            continue
        if quote:
            quote = None if ch == quote else quote
        elif ch in "/'\"":
            quote = ch
        elif ch == "|":
            out, buf, i = out + [buf.strip()], "", i + 1
            continue
        buf, i = buf + ch, i + 1
    return [c for c in out + [buf.strip()] if c]


def _filter(expr: str):
    """`@message like|=~|not like /re/ or "s"` terms joined by and/or, as a predicate (an or of ands)."""
    pos, ors = 0, [[]]
    while pos < len(expr):
        m = _TERM.match(expr, pos)
        if not m or (m.group(1) is None) != (pos == 0):
            raise ValueError(expr)
        lit, neg = m.group(3), m.group(2).lower().startswith("not")
        test = re.compile(lit[1:-1]).search if lit.startswith("/") else (lambda s, n=lit[1:-1]: n in s)
        if (m.group(1) or "").lower() == "or":
            ors.append([])
        ors[-1].append(lambda s, f=test, n=neg: bool(f(s)) != n)
        pos = m.end()
    return lambda s: any(all(p(s) for p in ands) for ands in ors)


def parse_query(qs: str) -> dict:
    """The Logs Insights subset this world serves; anything else is a MalformedQueryException, so no query is
    silently half-answered."""
    plan = {"filters": [], "sort": None, "limit": None, "fields": None, "stats": None}
    for cmd in _split(qs):
        name, _, rest = cmd.partition(" ")
        name, rest = name.lower(), rest.strip()
        try:
            if name == "filter":
                plan["filters"].append(_filter(rest))
            elif name == "fields":
                plan["fields"] = [f.strip() for f in rest.split(",") if f.strip() in _FIELDS]
            elif name == "sort":
                m = re.fullmatch(r"@timestamp(?:\s+(asc|desc))?", rest, re.IGNORECASE)
                plan["sort"] = (m.group(1) or "asc").lower()
            elif name == "limit":
                plan["limit"] = int(rest)
            elif name == "stats":
                m = _STATS.fullmatch(rest)
                by = [b.strip() for b in (m.group(3) or "").split(",") if b.strip()]
                bins = [_BIN.match(b) for b in by if b != "@log"]
                if any(b is None for b in bins) or len(bins) > 1:
                    raise ValueError(rest)
                plan["stats"] = {"name": m.group(2) or re.sub(r"\s", "", m.group(1)), "by": by, "bin_s":
                                 int(bins[0].group(1)) * {"s": 1, "m": 60, "h": 3600}[bins[0].group(2)] if bins else 0}
            else:
                raise ValueError(name)
        except (ValueError, AttributeError, re.error):
            raise AwsError("MalformedQueryException", f"unsupported in this environment: {cmd}; {SUPPORTED}",
                           400) from None
    return plan


class Telemetry:
    def __init__(self, world, cfg: dict):
        self.w = world
        self.cfg = cfg
        self.minutes: list[dict] = []
        self._mts: list[datetime] = []
        self.alarms: dict[str, dict] = {}
        self.queries: dict[str, dict] = {}
        self.outage_start = datetime.fromisoformat(cfg.get("outage_start", "2026-11-17T02:31:42-05:00"))
        self.extra_logs: list[tuple] = []
        self.alarm_since: dict[str, datetime] = {}
        self.groups: set[str] = set()
        self._gen = 0
        if world is None:
            return
        self.extra_logs = [(parse_time(e["t"], world.start_t), e["group"], e["message"]) for e in cfg.get("logs", [])]
        self.alarm_since = {k: parse_time(v, world.start_t) for k, v in (cfg.get("alarm_since") or {}).items()}
        self._gen = world.endpointd.next_gen
        self.groups = {r["params"]["logGroupName"] for r in world.scenario.get("aws_seed", [])
                       if r.get("service") == "logs" and r.get("operation") == "CreateLogGroup"}
        self.groups |= set(re.findall(r"'log_group': '([^']+)'", str(world.scenario["world"].get("fleets"))))

    def tick(self, t: datetime) -> None:
        self._planner_lines(t)
        if t.second >= self.w.tick_s and self.minutes:
            return
        s, ep = self.w.storm, self.w.endpointd
        m = {"t": t.replace(second=0, microsecond=0), "checkout": self.w.checkout_success_1m(),
             "p99": s.latency_ms(), "backlog": s.backlog, "offered": s.offered, "capacity": s.capacity,
             "regime": s.regime, "lw_tasks": s.lw_running, "conns": min(400, s.lw_running * 25),
             "outbox": s.outbox, "records": len(self.w.mirror.live(ep.ledger)),
             "ledger_correct": ep.correct(ep.ledger), "latch": (ep.latch_reasons() or [None])[0],
             "paused": self.w.levers()["consumer_paused"],
             "envoy": sum(1 for h in self.w.fleet.ingress() if not h.get("ready")),
             "recovered": [h for h in self.w.core_hosts if self.w.dns.resolves(h, t)]}
        if self.minutes and self.minutes[-1]["t"] == m["t"]:
            self.minutes[-1] = m
        else:
            self.minutes.append(m)
            self._mts.append(m["t"])
        p99_hot = len(self.minutes) >= 3 and all(x["p99"] > 1000 for x in self.minutes[-3:])
        states = {"checkout-api-CheckoutSuccessRate-critical": (m["checkout"] < 0.95, round(m["checkout"], 4)),
                  "ledger-writer-p99-high": (p99_hot, m["p99"]),
                  "webhooks-backlog-high": (m["backlog"] > 30000, round(m["backlog"])),
                  "endpointd-ApplyLagSeconds-high": (not m["ledger_correct"],
                                                     self.value("Vantleaf/Endpointd", "ApplyLagSeconds", {}, t)),
                  "ledger-ingress-EnvoyNotReady": (m["envoy"] > 0, m["envoy"])}
        for name, (on, dp) in states.items():
            v = "ALARM" if on else "OK"
            a = self.alarms.setdefault(name, {"state": None, "since": t, "dps": []})
            a["dps"] = (a["dps"] + [dp])[-3:]
            if a["state"] != v:
                since = self.alarm_since.get(name) if a["state"] is None and on else None
                a.update(state=v, since=since or t)

    def backfill(self, start: datetime) -> None:
        """Minute records from the outage to the episode start, which the world never ticks: the start's own state, with
        the outbox and webhook backlog growing from nothing at the outage as physics grows them."""
        span = (start - self.outage_start).total_seconds()
        if not self.minutes or span <= 0:
            return
        m0, t = self.minutes[0], self.outage_start.astimezone(start.tzinfo).replace(second=0, microsecond=0)
        rows = []
        while t < m0["t"]:
            f = max(0.0, (t - self.outage_start).total_seconds()) / span
            rows.append({**m0, "t": t, "outbox": m0["outbox"] * f, "backlog": m0["backlog"] * f})
            t += timedelta(minutes=1)
        self.minutes[:0], self._mts[:0] = rows, [r["t"] for r in rows]

    def _planner_lines(self, t: datetime) -> None:
        """The planner logs each plan it writes, with how many ledger records changed from the plan before."""
        ep = self.w.endpointd
        while self._gen < ep.next_gen:
            prev, cur = ep.archive.get(self._gen - 1), ep.archive.get(self._gen) or []
            recs = [{(r["ip"], r["weight"]) for r in x or []} for x in (prev, cur)]
            changes = len(recs[0] ^ recs[1]) if prev is not None else 0
            self.extra_logs.append((t, "/vl/platform/endpointd/planner",
                                    f"INFO planner gen={self._gen} endpoints={len(ep.order)} changes={changes}"))
            self._gen += 1

    def alarm_state(self, name: str) -> str:
        return (self.alarms.get(name) or {}).get("state") or "INSUFFICIENT_DATA"

    # ---- values ----
    def baseline_req_s(self) -> float:
        """Healthy ledger-writer load from the same physics: checkout, webhook arrivals and everything else."""
        c = self.w.storm.cfg
        return sum(float(c.get(k, d)) for k, d in (("checkout_req_s", 90), ("webhook_arrivals_per_s", 35),
                                                    ("other_req_s", 150)))

    def value(self, ns: str, metric: str, dims: dict, t: datetime) -> float | None:
        m = self._minute(t)
        pre = t < self.outage_start
        if m is None and not pre:
            return None
        key = (ns, metric)
        if key == ("Vantleaf/Checkout", "CheckoutSuccessRate"):
            return 0.997 if pre else round(m["checkout"], 4)
        if key == ("Vantleaf/Checkout", "OutboxPending"):
            return 0.0 if pre else float(round(m["outbox"]))
        if key == ("Vantleaf/LedgerWriter", "P99LatencyMs"):
            return 120.0 if pre else m["p99"]
        if key == ("Vantleaf/LedgerWriter", "Requests"):
            return round(self.baseline_req_s() * 60) if pre else round(m["offered"] * 60)
        if key == ("AWS/SQS", "ApproximateNumberOfMessagesVisible"):
            return 0.0 if pre else round(m["backlog"])
        if key == ("Vantleaf/Webhooks", "BacklogPerTask"):
            return 0.0 if pre else round(m["backlog"] / max(1, self.w.storm.dispatcher_running()), 1)
        if key == ("AWS/RDS", "DatabaseConnections"):
            return float(min(400, int(self.w.storm.cfg.get("ledger_writer_tasks", 12)) * 25) if pre else m["conns"])
        if key == ("Vantleaf/Endpointd", "EndpointRecordCount"):
            return 6.0 if pre else float(m["records"])
        if key == ("Vantleaf/Endpointd", "ApplyLagSeconds"):
            return 4.0 if pre or m["ledger_correct"] else (t - self.outage_start).total_seconds()
        if key == ("AWS/ECS", "RunningTaskCount"):
            svc = dims.get("ServiceName", "")
            if self.w.ecs is not None:
                return float(self.w.ecs.counts(svc)[1]) if svc in self.w.ecs.services else None
            return float(self.w.storm.lw_running if "ledger-writer" in svc else
                         self.w.storm.dispatcher_running() if "webhooks" in svc else 18)
        return None

    def _minute(self, t: datetime):
        i = bisect_right(self._mts, t.astimezone(self.w.start_t.tzinfo).replace(second=0, microsecond=0)) - 1
        return self.minutes[i] if i >= 0 else None

    def aggregate(self, ns, metric, dims, start, end, period, stats) -> list[tuple[datetime, dict]]:
        """Each statistic over every minute sample in [t, t+period), as CloudWatch aggregates a period."""
        start, end = _utc(start), min(_utc(end), self.w.now.astimezone(UTC))
        period = max(60, int(period or 60))
        out = []
        t = start.replace(second=0, microsecond=0)
        while t < end and len(out) < 1440:
            stop, x, vals = min(t + timedelta(seconds=period), end), t, []
            while x < stop:
                if (v := self.value(ns, metric, dims, x)) is not None:
                    vals.append(v)
                x += timedelta(minutes=1)
            if vals:
                out.append((t, {s: _stat(vals, s) for s in stats}))
            t += timedelta(seconds=period)
        return out

    # ---- dispatch ----
    def serve(self, service: str, op: str, p: dict, now: datetime):
        service = service.lower()
        if service in ("cloudwatch", "monitoring"):
            return self._cloudwatch(op, p, now)
        if service == "logs":
            return self._logs(op, p, now)
        if service == "dynamodb" and p.get("TableName") == OUTBOX:
            return self._outbox(op, p, now)
        if service == "sqs" and op == "GetQueueAttributes" and "vl-webhook-delivery" in str(p.get("QueueUrl")):
            dlq = str(p.get("QueueUrl")).endswith("-dlq")
            visible = self.w.storm.dlq if dlq else round(self.w.storm.backlog)
            busy = 0 if dlq or self.w.levers()["consumer_paused"] else min(visible,
                                                                            self.w.storm.dispatcher_running() * 10)
            attrs = {"ApproximateNumberOfMessages": str(visible), "ApproximateNumberOfMessagesNotVisible": str(busy),
                     "ApproximateNumberOfMessagesDelayed": "0", "VisibilityTimeout": "0" if not dlq else "30",
                     "MessageRetentionPeriod": "345600"}
            want = p.get("AttributeNames") or ["All"]
            return {"Attributes": attrs if "All" in want else {k: v for k, v in attrs.items() if k in want}}
        return None

    def _cloudwatch(self, op: str, p: dict, now: datetime):
        if op == "GetMetricData":
            res = []
            for q in p.get("MetricDataQueries", []):
                ms = (q.get("MetricStat") or {})
                met = ms.get("Metric") or {}
                dims = {d["Name"]: d["Value"] for d in met.get("Dimensions", [])}
                stat = ms.get("Stat") or "Average"
                pts = self.aggregate(met.get("Namespace"), met.get("MetricName"), dims, p["StartTime"], p["EndTime"],
                                     ms.get("Period", 60), [stat])
                pts = pts if p.get("ScanBy") == "TimestampAscending" else pts[::-1]
                res.append({"Id": q.get("Id"), "Label": q.get("Label") or met.get("MetricName"),
                            "Timestamps": [t for t, _ in pts], "Values": [v[stat] for _, v in pts],
                            "StatusCode": "Complete"})
            return {"MetricDataResults": res, "Messages": []}
        if op == "GetMetricStatistics":
            dims = {d["Name"]: d["Value"] for d in p.get("Dimensions", [])}
            if self.value(p.get("Namespace"), p.get("MetricName"), dims, now) is None:
                return None
            ext = p.get("ExtendedStatistics") or []
            stats = p.get("Statistics") or ([] if ext else ["Average"])
            pts = self.aggregate(p["Namespace"], p["MetricName"], dims, p["StartTime"], p["EndTime"],
                                 p.get("Period", 60), stats + ext)
            return {"Label": p["MetricName"], "Datapoints": [
                {"Timestamp": t, **{s: v[s] for s in stats},
                 **({"ExtendedStatistics": {e: v[e] for e in ext}} if ext else {}), "Unit": "None"}
                for t, v in pts[::-1]]}
        if op == "ListMetrics":
            names = [("Vantleaf/Checkout", "CheckoutSuccessRate", [{"Name": "Service", "Value": "checkout-api"}]),
                     ("Vantleaf/Checkout", "OutboxPending", [{"Name": "Service", "Value": "checkout-api"}]),
                     ("Vantleaf/LedgerWriter", "P99LatencyMs", [{"Name": "Service", "Value": "ledger-writer"}]),
                     ("Vantleaf/LedgerWriter", "Requests", [{"Name": "Service", "Value": "ledger-writer"}]),
                     ("Vantleaf/Webhooks", "BacklogPerTask", [{"Name": "Service", "Value": "webhooks-dispatcher"}]),
                     ("Vantleaf/Endpointd", "EndpointRecordCount",
                      [{"Name": "Endpoint", "Value": "ledger.internal.vantleaf.io"}]),
                     ("Vantleaf/Endpointd", "ApplyLagSeconds", [{"Name": "Environment", "Value": "prod"}]),
                     ("AWS/SQS", "ApproximateNumberOfMessagesVisible",
                      [{"Name": "QueueName", "Value": "vl-webhook-delivery"}]),
                     ("AWS/RDS", "DatabaseConnections", [{"Name": "DBClusterIdentifier", "Value": "vl-ledger-db"}])]
            ns = p.get("Namespace")
            return {"Metrics": [{"Namespace": n, "MetricName": m, "Dimensions": d} for n, m, d in names
                                if not ns or ns == n and (not p.get("MetricName") or p["MetricName"] == m)]}
        if op == "DescribeAlarms":
            want = set(p.get("AlarmNames") or [])
            prefix = p.get("AlarmNamePrefix", "")
            out = []
            for name, ns, metric, cmp, thr, periods in ALARMS:
                if want and name not in want or not name.startswith(prefix):
                    continue
                a = self.alarms.get(name) or {"state": "INSUFFICIENT_DATA", "since": now, "dps": []}
                if p.get("StateValue") and p["StateValue"] != a["state"]:
                    continue
                dps = a["dps"][-periods:]
                reason = (f"Threshold Crossed: {len(dps)} datapoint{'' if len(dps) == 1 else 's'} "
                          f"[{', '.join(_fmt(v) for v in dps)}] was {'' if a['state'] == 'ALARM' else 'not '}"
                          f"{'less' if cmp.startswith('Less') else 'greater'} than the threshold ({_fmt(thr)}).")
                out.append({"AlarmName": name, "AlarmArn": f"arn:aws:cloudwatch:us-east-1:{ACCOUNT}:alarm:{name}",
                            "Namespace": ns, "MetricName": metric, "ComparisonOperator": cmp, "Threshold": thr,
                            "StateValue": a["state"], "StateUpdatedTimestamp": a["since"], "StateReason": reason,
                            "Period": 60, "EvaluationPeriods": periods, "DatapointsToAlarm": periods,
                            "Statistic": "Average", "ActionsEnabled": True})
            return {"MetricAlarms": out, "CompositeAlarms": []}
        return None

    def _logs(self, op: str, p: dict, now: datetime):
        if op == "FilterLogEvents":
            group = self._known(p.get("logGroupName") or p.get("logGroupIdentifier"))
            evs = self.log_events(group, p.get("startTime"), p.get("endTime"), p.get("filterPattern"), now,
                                  int(p.get("limit", 200)))
            return {"events": evs, "searchedLogStreams": []}
        if op == "StartQuery":
            groups = [self._known(g) for g in p.get("logGroupNames") or p.get("logGroupIdentifiers") or
                      ([p["logGroupName"]] if p.get("logGroupName") else [])]
            if not groups:
                raise AwsError("InvalidParameterException", "Specify logGroupName, logGroupNames or "
                                                            "logGroupIdentifiers.", 400)
            qid = f"q-{len(self.queries) + 1:06d}"
            q = {**p, "groups": groups, "plan": parse_query(p.get("queryString", "")), "at": now}
            self.queries[qid] = {**q, "result": self.run_query(q, now)}
            return {"queryId": qid}
        if op == "GetQueryResults":
            q = self.queries.get(p.get("queryId"))
            return None if q is None else q["result"]
        return None

    def _known(self, g) -> str:
        """Only the log groups the scenario created answer, as in CloudWatch Logs."""
        g = _group(g)
        if g not in self.groups and not any(x[1] == g for x in self.extra_logs):
            raise AwsError("ResourceNotFoundException", "The specified log group does not exist.", 400)
        return g

    def _outbox(self, op: str, p: dict, now: datetime):
        """vl-ledger-outbox reads show the checkout outbox that physics holds; writes stay with the emulator."""
        if now < self.outage_start - timedelta(minutes=9):
            return None
        n = round(self.w.storm.outbox)
        if op == "DescribeTable":
            return {"Table": {"TableName": OUTBOX, "TableStatus": "ACTIVE", "ItemCount": n, "TableSizeBytes": n * 350,
                              "KeySchema": [{"AttributeName": "payment_id", "KeyType": "HASH"}],
                              "AttributeDefinitions": [{"AttributeName": "payment_id", "AttributeType": "S"}],
                              "BillingModeSummary": {"BillingMode": "PAY_PER_REQUEST"},
                              "TableArn": f"arn:aws:dynamodb:us-east-1:{ACCOUNT}:table/{OUTBOX}"}}
        if op in ("Scan", "Query") and p.get("Select") == "COUNT":
            return {"Count": n, "ScannedCount": n}
        if op != "Scan":
            return None
        start = (p.get("ExclusiveStartKey") or {}).get("payment_id", {}).get("S")
        lo = int(start[3:9], 16) + 1 if start else 0
        hi = min(n, lo + min(int(p.get("Limit") or 1000), 1000))
        items = [self._outbox_item(i) for i in range(lo, hi)]
        out = {"Items": items, "Count": len(items), "ScannedCount": len(items)}
        if hi < n:
            out["LastEvaluatedKey"] = {"payment_id": items[-1]["payment_id"]}
        return out

    def _outbox_item(self, i: int) -> dict:
        h = hashlib.sha256(f"{self.w.seed}|outbox|{i}".encode()).hexdigest()
        at = min(self.outage_start + timedelta(seconds=i / 15), self.w.now)
        return {"payment_id": {"S": f"pi_{i:06x}{h[:18]}"}, "status": {"S": "PENDING_POSTING"},
                "authorized_at": {"S": iso(at)}}

    # ---- logs ----
    def lines(self, group: str, t: datetime, host: str | None = None) -> list[str]:
        """The app log lines `group` writes in the minute of `t`, across the fleet or, given `host`, on that host.

        They read only that minute's record, never live state, so log history never changes after the fact."""
        w = self.w
        if t < self.outage_start or not self.minutes:
            return []
        m = self._minute(t) or self.minutes[0]
        n_unrec = len(w.core_hosts) - len(m["recovered"])
        if host is not None and host in w.core_hosts:
            n_unrec = int(host not in m["recovered"])
        kind = APP_GROUPS.get(group) or ("endpointd" if group.startswith("/vl/platform/endpointd/") else None)
        if kind == "checkout":
            out = []
            if n_unrec:
                out.append("ERROR c.v.ledger.LedgerClient - post failed: java.net.UnknownHostException: "
                           "ledger.internal.vantleaf.io: Name or service not known"
                           + ("" if host else f" ({n_unrec} hosts)"))
                out.append("WARN c.v.ledger.LedgerClient - CircuitBreakerOpenException: ledger-client breaker OPEN "
                           "(failure rate 100.0% > 50.0%)")
            if m["regime"] == "collapsed":
                out.append("ERROR c.v.ledger.LedgerClient - post failed: java.net.SocketTimeoutException: "
                           "Read timed out after 2000ms (ledger.internal.vantleaf.io)")
                out.append("ERROR c.v.ledger.LedgerClient - HTTP 503 from ledger-writer: db pool exhausted")
            return out
        if kind == "dispatcher":
            out = []
            if n_unrec and not m["paused"]:
                out.append("ERROR dispatcher - deliver failed: getaddrinfo ENOTFOUND ledger.internal.vantleaf.io; "
                           "nack visibility=0")
            if m["regime"] == "collapsed" and not m["paused"]:
                out.append("ERROR dispatcher - ledger post timeout after 2000ms; retrying immediately")
            return out
        if kind == "ledger":
            tasks = max(1, m["lw_tasks"])
            if m["regime"] == "collapsed":
                waiting = round(1.5 * m["offered"] / tasks)
                return [f"WARN db pool exhausted: waited 1500ms for connection (active=25 idle=0 waiting={waiting})",
                        "WARN client cancelled after 2000ms; query still running (no context propagation, PAY-655)"]
            active = min(25, math.ceil(25 * m["offered"] / max(1.0, m["capacity"])))
            line = (f"INFO inbound {m['offered'] / tasks:.1f} req/s per task (1m avg, baseline "
                    f"~{self.baseline_req_s() / tasks:.0f}); pool active={active} idle={25 - active} waiting=0")
            return [line + ("" if m["recovered"] else " (callers: checkout-api 0, webhooks-dispatcher 0)")]
        if kind == "endpointd" and not group.endswith("/planner") and m["latch"]:
            return [f"WARN halt latch engaged: {m['latch']}; writing nothing"]
        return []

    def log_events(self, group, start, end, pattern, now, limit) -> list[dict]:
        start_t = _utc(start) if start else (now - timedelta(minutes=30)).astimezone(UTC)
        end_t = min(_utc(end) if end else now.astimezone(UTC), now.astimezone(UTC))
        gen_t = max(start_t, self.outage_start.astimezone(UTC) - timedelta(minutes=5))
        needle = (pattern or "").strip().strip('"').strip("'").lower()
        out = []
        t = gen_t.replace(second=42, microsecond=0)
        if t < gen_t:
            t += timedelta(minutes=1)
        while t <= end_t and len(out) < limit:
            for i, line in enumerate(self.lines(group, t)):
                if needle and needle not in line.lower():
                    continue
                ms = int(t.timestamp() * 1000) + i
                out.append({"logStreamName": f"{group.rsplit('/', 1)[-1]}/i-0{abs(hash(group)) % 10**7:07d}",
                            "timestamp": ms, "message": line, "ingestionTime": ms + 400, "eventId": f"{ms}{i}"})
            t += timedelta(minutes=1)
        for ts, g, msg in self.extra_logs:
            if g == group and start_t <= ts.astimezone(UTC) <= end_t and (not needle or needle in msg.lower()):
                ms = int(ts.timestamp() * 1000)
                out.append({"logStreamName": "run", "timestamp": ms, "message": msg, "ingestionTime": ms,
                            "eventId": str(ms)})
        return sorted(out, key=lambda e: e["timestamp"])[:limit]

    def run_query(self, q: dict, now: datetime) -> dict:
        plan, groups = q["plan"], q["groups"]
        rows = [(g, e) for g in groups
                for e in self.log_events(g, q.get("startTime"), q.get("endTime"), None, now, 10000)
                if all(f(e["message"]) for f in plan["filters"])]
        limit = min(plan["limit"] or 10000, int(q.get("limit") or 10000))
        if plan["stats"]:
            st, buckets = plan["stats"], {}
            for g, e in rows:
                k = (e["timestamp"] // 1000 // st["bin_s"] * st["bin_s"] if st["bin_s"] else 0,
                     g if "@log" in st["by"] else "")
                buckets[k] = buckets.get(k, 0) + 1
            results = [[{"field": f, "value": g if f == "@log" else
                         datetime.fromtimestamp(b, UTC).strftime("%Y-%m-%d %H:%M:%S.000")} for f in st["by"]]
                       + [{"field": st["name"], "value": str(n)}] for (b, g), n in sorted(buckets.items())][:limit]
        else:
            rows.sort(key=lambda r: r[1]["timestamp"], reverse=plan["sort"] != "asc")
            fields = plan["fields"] or ["@timestamp", "@message"] + (["@log"] if len(groups) > 1 else [])
            val = {"@timestamp": lambda g, e: datetime.fromtimestamp(e["timestamp"] / 1000, UTC)
                   .strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], "@message": lambda g, e: e["message"],
                   "@log": lambda g, e: g, "@logStream": lambda g, e: e["logStreamName"]}
            results = [[{"field": f, "value": val[f](g, e)} for f in fields] for g, e in rows[:limit]]
        return {"status": "Complete", "results": results,
                "statistics": {"recordsMatched": float(len(rows)), "recordsScanned": float(len(rows) * 4),
                               "bytesScanned": float(len(rows) * 600)}}

    def export(self) -> dict:
        return {"alarms": {k: {"state": v["state"], "since": iso(v["since"])} for k, v in self.alarms.items()}}
