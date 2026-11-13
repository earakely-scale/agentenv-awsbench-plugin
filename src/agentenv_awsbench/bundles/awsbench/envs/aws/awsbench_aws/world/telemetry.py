"""AWS reads that physics owns: CloudWatch metrics and alarms, CloudWatch Logs and SQS queue depth."""

import re
from datetime import UTC, datetime, timedelta

from .util import iso

ALARMS = [
    ("CheckoutSuccessRate-critical", "Vantleaf/Checkout", "CheckoutSuccessRate", "LessThanThreshold", 0.95),
    ("ledger-writer-p99-high", "Vantleaf/LedgerWriter", "P99LatencyMs", "GreaterThanThreshold", 1000),
    ("webhooks-backlog-high", "AWS/SQS", "ApproximateNumberOfMessagesVisible", "GreaterThanThreshold", 30000),
    ("endpointd-ApplyLagSeconds-high", "Vantleaf/Endpointd", "ApplyLagSeconds", "GreaterThanThreshold", 300),
    ("ledger-ingress-EnvoyNotReady", "Vantleaf/LedgerIngress", "EnvoyNotReady", "GreaterThanThreshold", 0),
]


def _utc(t) -> datetime:
    if isinstance(t, (int, float)):
        return datetime.fromtimestamp(t / 1000 if t > 1e11 else t, UTC)
    if isinstance(t, str):
        return datetime.fromisoformat(t.replace("Z", "+00:00"))
    return t.astimezone(UTC)


class Telemetry:
    def __init__(self, world, cfg: dict):
        self.w = world
        self.cfg = cfg
        self.minutes: list[dict] = []
        self.alarms: dict[str, dict] = {}
        self.extra_logs: list[tuple] = []
        self.queries: dict[str, dict] = {}
        self.outage_start = datetime.fromisoformat(cfg.get("outage_start", "2026-11-17T02:31:42-05:00"))

    def tick(self, t: datetime) -> None:
        if t.second >= self.w.tick_s and self.minutes:
            return
        s = self.w.storm
        m = {"t": t.replace(second=0, microsecond=0), "checkout": self.w.checkout_success_1m(),
             "p99": s.latency_ms(), "backlog": s.backlog, "offered": s.offered, "regime": s.regime,
             "lw_tasks": s.lw_running, "conns": min(400, s.lw_running * 25 + (75 if s.regime == "collapsed" else 0)),
             "records": len(self.w.mirror.live(self.w.endpointd.ledger)),
             "ledger_correct": self.w.endpointd.correct(self.w.endpointd.ledger),
             "recovered": [h for h in self.w.core_hosts if self.w.dns.resolves(h, t)]}
        if self.minutes and self.minutes[-1]["t"] == m["t"]:
            self.minutes[-1] = m
        else:
            self.minutes.append(m)
        p99_hot = len(self.minutes) >= 3 and all(x["p99"] > 1000 for x in self.minutes[-3:])
        states = {"CheckoutSuccessRate-critical": m["checkout"] < 0.95, "ledger-writer-p99-high": p99_hot,
                  "webhooks-backlog-high": m["backlog"] > 30000,
                  "endpointd-ApplyLagSeconds-high": not m["ledger_correct"],
                  "ledger-ingress-EnvoyNotReady": any(not h.get("ready") for h in self.w.fleet.ingress())}
        for name, on in states.items():
            v = "ALARM" if on else "OK"
            a = self.alarms.setdefault(name, {"state": None, "since": t})
            if a["state"] != v:
                a.update(state=v, since=t)

    def alarm_state(self, name: str) -> str:
        return (self.alarms.get(name) or {}).get("state") or "INSUFFICIENT_DATA"

    # ---- values ----
    def value(self, ns: str, metric: str, dims: dict, t: datetime) -> float | None:
        m = self._minute(t)
        pre = t < self.outage_start
        if m is None and not pre:
            return None
        key = (ns, metric)
        if key == ("Vantleaf/Checkout", "CheckoutSuccessRate"):
            return 0.997 if pre else round(m["checkout"], 4)
        if key == ("Vantleaf/LedgerWriter", "P99LatencyMs"):
            return 120.0 if pre else m["p99"]
        if key == ("Vantleaf/LedgerWriter", "Requests"):
            return 1150.0 if pre else round(m["offered"] * 60)
        if key == ("AWS/SQS", "ApproximateNumberOfMessagesVisible"):
            return 0.0 if pre else round(m["backlog"])
        if key == ("Vantleaf/Webhooks", "BacklogPerTask"):
            return 0.0 if pre else round(m["backlog"] / max(1, self.w.storm.dispatcher_running()), 1)
        if key == ("AWS/RDS", "DatabaseConnections"):
            return 180.0 if pre else float(m["conns"])
        if key == ("Vantleaf/Endpointd", "EndpointRecordCount"):
            return 6.0 if pre else float(m["records"])
        if key == ("Vantleaf/Endpointd", "ApplyLagSeconds"):
            return 4.0 if pre or m["ledger_correct"] else (t - self.outage_start).total_seconds()
        if key == ("AWS/ECS", "RunningTaskCount"):
            svc = dims.get("ServiceName", "")
            return float(self.w.storm.lw_running if "ledger-writer" in svc else
                         self.w.storm.dispatcher_running() if "webhooks" in svc else 18)
        return None

    def _minute(self, t: datetime):
        t = t.astimezone(self.w.start_t.tzinfo).replace(second=0, microsecond=0)
        best = None
        for m in self.minutes:
            if m["t"] <= t:
                best = m
            else:
                break
        return best

    def series(self, ns, metric, dims, start, end, period) -> tuple[list, list]:
        start, end = _utc(start), min(_utc(end), self.w.now.astimezone(UTC))
        period = max(60, int(period or 60))
        ts, vs = [], []
        t = start.replace(second=0, microsecond=0)
        while t < end and len(ts) < 1440:
            v = self.value(ns, metric, dims, t)
            if v is not None:
                ts.append(t)
                vs.append(v)
            t += timedelta(seconds=period)
        return ts[::-1], vs[::-1]

    # ---- dispatch ----
    def serve(self, service: str, op: str, p: dict, now: datetime):
        service = service.lower()
        if service in ("cloudwatch", "monitoring"):
            if op == "GetMetricData":
                res = []
                for q in p.get("MetricDataQueries", []):
                    ms = (q.get("MetricStat") or {})
                    met = ms.get("Metric") or {}
                    dims = {d["Name"]: d["Value"] for d in met.get("Dimensions", [])}
                    ts, vs = self.series(met.get("Namespace"), met.get("MetricName"), dims, p["StartTime"],
                                         p["EndTime"], ms.get("Period", 60))
                    res.append({"Id": q.get("Id"), "Label": q.get("Label") or met.get("MetricName"),
                                "Timestamps": ts, "Values": vs, "StatusCode": "Complete"})
                return {"MetricDataResults": res, "Messages": []}
            if op == "GetMetricStatistics":
                dims = {d["Name"]: d["Value"] for d in p.get("Dimensions", [])}
                if self.value(p.get("Namespace"), p.get("MetricName"), dims, now) is None:
                    return None
                ts, vs = self.series(p["Namespace"], p["MetricName"], dims, p["StartTime"], p["EndTime"],
                                     p.get("Period", 60))
                stats = p.get("Statistics") or ["Average"]
                return {"Label": p["MetricName"], "Datapoints": [
                    {"Timestamp": t, **{s: v for s in stats}, "Unit": "None"} for t, v in zip(ts, vs)]}
            if op == "ListMetrics":
                names = [("Vantleaf/Checkout", "CheckoutSuccessRate", [{"Name": "Service", "Value": "checkout-api"}]),
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
                for name, ns, metric, cmp, thr in ALARMS:
                    if want and name not in want or not name.startswith(prefix):
                        continue
                    a = self.alarms.get(name) or {"state": "INSUFFICIENT_DATA", "since": now}
                    if p.get("StateValue") and p["StateValue"] != a["state"]:
                        continue
                    out.append({"AlarmName": name, "AlarmArn": f"arn:aws:cloudwatch:us-east-1:000000000000:alarm:{name}",
                                "Namespace": ns, "MetricName": metric, "ComparisonOperator": cmp, "Threshold": thr,
                                "StateValue": a["state"], "StateUpdatedTimestamp": a["since"],
                                "StateReason": f"Threshold Crossed: datapoint {'breached' if a['state'] == 'ALARM' else 'within'} the threshold ({thr})",
                                "Period": 60, "EvaluationPeriods": 3, "Statistic": "Average", "ActionsEnabled": True})
                return {"MetricAlarms": out, "CompositeAlarms": []}
            return None
        if service == "logs":
            if op == "FilterLogEvents":
                evs = self.log_events(p.get("logGroupName") or p.get("logGroupIdentifier", ""), p.get("startTime"),
                                      p.get("endTime"), p.get("filterPattern"), now, int(p.get("limit", 200)))
                return {"events": evs, "searchedLogStreams": []}
            if op == "StartQuery":
                qid = f"q-{len(self.queries) + 1:06d}"
                self.queries[qid] = {**p, "at": now}
                return {"queryId": qid}
            if op == "GetQueryResults":
                q = self.queries.get(p.get("queryId"))
                if q is None:
                    return None
                return self.run_query(q, now)
            return None
        if service == "sqs" and op == "GetQueueAttributes" and "vl-webhook-delivery" in str(p.get("QueueUrl")):
            dlq = str(p.get("QueueUrl")).endswith("-dlq")
            visible = self.w.storm.dlq if dlq else round(self.w.storm.backlog)
            return {"Attributes": {"ApproximateNumberOfMessages": str(visible),
                                   "ApproximateNumberOfMessagesNotVisible": "0" if dlq else str(
                                       min(visible, self.w.storm.dispatcher_running() * 10)),
                                   "ApproximateNumberOfMessagesDelayed": "0", "VisibilityTimeout": "0" if not dlq else "30",
                                   "MessageRetentionPeriod": "345600"}}
        return None

    # ---- logs ----
    def _lines(self, group: str, t: datetime) -> list[str]:
        w = self.w
        if t < self.outage_start:
            return []
        m = self._minute(t) or {"recovered": [], "regime": "healthy"}
        n_unrec = len(w.core_hosts) - len(m["recovered"])
        if group.endswith("checkout-api"):
            out = []
            if n_unrec:
                out.append(f"ERROR c.v.ledger.LedgerClient - post failed: java.net.UnknownHostException: "
                           f"ledger.internal.vantleaf.io: Name or service not known ({n_unrec} hosts)")
                out.append("WARN c.v.ledger.LedgerClient - CircuitBreakerOpenException: ledger-client breaker OPEN "
                           "(failure rate 100.0% > 50.0%)")
            if m["regime"] == "collapsed":
                out.append("ERROR c.v.ledger.LedgerClient - post failed: java.net.SocketTimeoutException: "
                           "Read timed out after 2000ms (ledger.internal.vantleaf.io)")
                out.append("ERROR c.v.ledger.LedgerClient - HTTP 503 from ledger-writer: db pool exhausted")
            return out
        if group.endswith("webhooks-dispatcher"):
            out = []
            if n_unrec:
                out.append("ERROR dispatcher - deliver failed: getaddrinfo ENOTFOUND ledger.internal.vantleaf.io; "
                           "nack visibility=0")
            if m["regime"] == "collapsed":
                out.append("ERROR dispatcher - ledger post timeout after 2000ms; retrying immediately")
            return out
        if group.endswith("ledger-writer"):
            if m["regime"] == "collapsed":
                return ["WARN db pool exhausted: waited 1500ms for connection (active=25 idle=0 waiting=212)",
                        "WARN client cancelled after 2000ms; query still running (no context propagation, PAY-655)"]
            return []
        if "/endpointd/" in group:
            comp = group.rsplit("/", 1)[-1]
            ep = w.endpointd
            if comp != "planner" and ep.engaged():
                return [f"WARN halt latch engaged: {ep.latch_reasons()[0]}; writing nothing"]
            return []
        return []

    def log_events(self, group, start, end, pattern, now, limit) -> list[dict]:
        start_t = _utc(start) if start else (now - timedelta(minutes=30)).astimezone(UTC)
        end_t = min(_utc(end) if end else now.astimezone(UTC), now.astimezone(UTC))
        start_t = max(start_t, self.outage_start.astimezone(UTC) - timedelta(minutes=5))
        needle = (pattern or "").strip().strip('"').strip("'")
        out = []
        t = start_t.replace(second=42, microsecond=0)
        if t < start_t:
            t += timedelta(minutes=1)
        while t <= end_t and len(out) < limit:
            for i, line in enumerate(self._lines(group, t)):
                if needle and needle.lower() not in line.lower():
                    continue
                ms = int(t.timestamp() * 1000) + i
                out.append({"logStreamName": f"{group.rsplit('/', 1)[-1]}/i-0{abs(hash(group)) % 10**7:07d}",
                            "timestamp": ms, "message": line, "ingestionTime": ms + 400, "eventId": f"{ms}{i}"})
            t += timedelta(minutes=1)
        for ts, g, msg in self.extra_logs:
            if g == group and start_t <= ts.astimezone(UTC) <= end_t and (not needle or needle in msg):
                ms = int(ts.timestamp() * 1000)
                out.append({"logStreamName": "run", "timestamp": ms, "message": msg, "ingestionTime": ms,
                            "eventId": str(ms)})
        return sorted(out, key=lambda e: e["timestamp"])[:limit]

    def run_query(self, q: dict, now: datetime) -> dict:
        groups = q.get("logGroupNames") or [q.get("logGroupName")] if q.get("logGroupName") else q.get(
            "logGroupNames", [])
        qs = q.get("queryString", "")
        m = re.search(r"@message\s+like\s+/([^/]+)/", qs) or re.search(r"@message\s+like\s+['\"]([^'\"]+)['\"]", qs)
        needle = m.group(1) if m else None
        rows = []
        for g in groups or []:
            rows += [(g, e) for e in self.log_events(g, q.get("startTime"), q.get("endTime"), needle, now, 10000)]
        if "stats" in qs and "count" in qs:
            results = [[{"field": "count()", "value": str(len(rows))}]]
        else:
            results = [[{"field": "@timestamp", "value": datetime.fromtimestamp(e["timestamp"] / 1000, UTC)
                         .strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]}, {"field": "@log", "value": g},
                        {"field": "@message", "value": e["message"]}] for g, e in rows[-int(q.get("limit", 100)):]]
        return {"status": "Complete", "results": results,
                "statistics": {"recordsMatched": float(len(rows)), "recordsScanned": float(len(rows) * 4),
                               "bytesScanned": float(len(rows) * 600)}}

    def export(self) -> dict:
        return {"alarms": {k: {"state": v["state"], "since": iso(v["since"])} for k, v in self.alarms.items()}}
