"""T03 telemetry served by the world (docs/T03-build.md §1.9): CloudWatch metrics and alarms, CloudWatch Logs, the
canary Lambda and the streamroute-leases table, from per-minute samples of the mechanics. A sample at minute `M` is the
state at `M` and reads as the datapoint of [M, M+60), published once the minute is over. Alarms start from the
scenario's `alarm_history_at_start` and are evaluated each minute from then on, as CloudWatch evaluates them. CWAgent's
mem_used_percent reads `cwagent_extra_pct` above the hosts' own figure: it also counts the page cache's dirty pages."""

import json
import re
from bisect import bisect_left, bisect_right
from datetime import UTC, datetime

from ..account import ACCOUNT
from ..errors import AwsError
from .t03_fleet import BROKEN, EJECTED, SERVING, STALE
from .telemetry import _fmt, _group, _stat, _utc, parse_query
from .util import parse_time, unit

ASG_DIM = ("AutoScalingGroupName", "vl-streamroute-asg")
STREAM = ("StreamName", "vl-payment-events")
ALBS = {"vl-checkout-alb": ("app/vl-checkout-alb/5c8e2f1a9d3b7e40", 0.38), "vl-api-alb": ("app/vl-api-alb/8a1f3c6e2b9d4f71",
                                                                                          0.61)}
HISTORY = {"streamroute-mem-used-high": ["ALARM", "07:06:00"], "streamroute-route-errors-high": ["ALARM", "07:51:00"],
           "kinesis-vl-payment-events-iterator-age-high": ["ALARM", "08:02:00"],
           "vl-ledger-e2e-canary-failing": ["ALARM", "08:09:00"]}
LEASES = "streamroute-leases"
CANARY_FN = "ledger-e2e-canary"
CMP = {"GreaterThanThreshold": (float.__gt__, "greater than"), "GreaterThanOrEqualToThreshold": (float.__ge__,
       "greater than or equal to"), "LessThanThreshold": (float.__lt__, "less than"),
       "LessThanOrEqualToThreshold": (float.__le__, "less than or equal to")}
STREAMS = {"/vl/prod/srcoord": "srcoord-1", "/vl/prod/ledger-writer": "ecs/ledger-writer/7f3c2a91e5d04b6f8a1e",
           "/vl/prod/bizmetrics": "bizmetrics-0", "/vl/prod/cellmap": "cellmap-admin-01",
           "/aws/lambda/ledger-e2e-canary": "2026/11/16/[$LATEST]5b1e09c4d7a2483f9e61c0a8f3d2b7e4"}
LW_START = ("07:32:04", ("INFO ledger-writer 3.41.0 started (task definition ledger-writer:234); reading streamroute "
                         "cells lc-01..lc-08"))
NO_MAP = ("WARN [route-worker] c.v.streamroute.route.Router - route.no_shardmap: no valid shard map; batch held, "
          "records stay in vl-payment-events")
RENEW_SLOW = ("WARN [lease-renewer] c.v.streamroute.lease.LeaseRenewer - lease.renew_slow: renewal took 2.8s (shard map "
              "STALE, rebuild stuck)")
PAGE = 1000


def _ms(t: float) -> int:
    return int(t * 1000)


def _z(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+0000"


def _s(t) -> float:
    return _utc(t).timestamp()


def _matcher(pattern: str | None):
    """CloudWatch Logs filter terms: all plain or quoted terms, any `?term`, no `-term`."""
    terms = re.findall(r'"[^"]*"|\S+', (pattern or "").strip())
    need = [t.strip('"') for t in terms if t[0] not in "?-"]
    anyof, nope = [t[1:].strip('"') for t in terms if t[0] == "?"], [t[1:].strip('"') for t in terms if t[0] == "-"]
    return lambda s: (all(n in s for n in need) and (not anyof or any(a in s for a in anyof))
                      and not any(n in s for n in nope))


class Telemetry:
    def __init__(self, m, cfg: dict):
        self.m, self.cfg = m, cfg
        self.samples: list[dict] = []
        self.ts: list[float] = []
        self.defs: dict[str, dict] = {}
        self.alarms: dict[str, dict] = {}
        self.history: list[dict] = []
        self.timeline: list[list] = []
        self.queries: dict[str, dict] = {}
        self.counter: list[int] = []
        self.started = False
        seed = m.w.scenario.get("aws_seed") or []
        self.groups = [r["params"]["logGroupName"] for r in seed
                       if r.get("service") == "logs" and r.get("operation") == "CreateLogGroup"]
        self.seed_defs = {r["params"]["AlarmName"]: self._arns(r["params"]) for r in seed
                          if r.get("service") == "cloudwatch" and r.get("operation") == "PutMetricAlarm"}
        self.created = parse_time(m.w.scenario.get("seeded_at", "2026-09-01T11:00:00-04:00"), m.w.start_t).timestamp()
        self.series = self._series()

    @staticmethod
    def _arns(params: dict) -> dict:
        return json.loads(re.sub(r"\$\{ref:sns\.topic/([^.}]+)\.TopicArn\}",
                                 lambda x: f"arn:aws:sns:us-east-1:{ACCOUNT}:{x[1]}", json.dumps(params)))

    # ---- samples ----
    def record(self, ts: float) -> None:
        m, f, flow = self.m, self.m.fleet, self.m.flow
        if not self.counter:
            self.counter = [18000 + int(700 * unit("t03-presim", "lease", s)) for s in range(flow.p["shards"])]
            self._routed = flow.routed
        procs = [h for h in f.hosts.values() if h.proc and not h.terminated]
        owners = {o: f.hosts.get(o) for o in set(flow.owner)}
        serving = [bool(h and h.proc and h.state in SERVING) for h in map(owners.get, flow.owner)]
        self.counter = [c + 6 * ok for c, ok in zip(self.counter, serving)]
        lags = list(flow.cell_lags(ts))
        demand = flow.demand(ts)
        broken = sum(w for w, ok in zip(flow.w, serving) if not ok) / len(flow.w)
        cm = f.cell
        s = {"canary": float(flow.canary_ok(f, ts)), "cells": lags, "wm": ts - max(lags),
             "routed": 0, "incoming": round(demand * 60),
             "errors": round(demand * 60 * broken * self.cfg.get("route_error_rate", 0.012) + 3 * unit(m.w.seed,
                                                                                                    "err", ts)),
             "valid": sum(1 for h in procs if h.state in SERVING), "ejected": sum(1 for h in f.hosts.values()
                                                                                   if h.state == EJECTED),
             "peers": round(sum(h.known for h in procs) / max(1, len(procs)), 1),
             "mem": round(sum(m.cmds.mem_pct(h) for h in procs) / max(1, len(procs)) + self.cfg.get("cwagent_extra_pct",
                                                                                              0.3), 2),
             "broken": tuple(h.name for h in procs if h.state == BROKEN),
             "stale": tuple(h.name for h in procs if h.state == STALE),
             "gc": procs[int(ts // 60) % len(procs)].name if procs else None,
             "boot": (len(cm.in_flight), len(cm.retrying), float(cm.shed(ts)), cm.throttled)}
        if self.samples:
            self.samples[-1]["routed"] = round(flow.routed - self._routed)
        self._routed = flow.routed
        if self.ts and self.ts[-1] == ts:
            self.samples[-1] = s
        else:
            self.samples.append(s)
            self.ts.append(ts)
        if self.started:
            self.evaluate(ts)

    def _sample(self, t: float) -> dict | None:
        """The minute's sample; before the presimulation, the healthy 06:00 fleet at that hour's demand."""
        i = bisect_right(self.ts, t) - 1
        if i < 0 and self.ts:
            flow, s0 = self.m.flow, self.samples[0]
            k = flow.demand(flow.midnight + (t - flow.midnight) % 86400) / flow.demand(self.ts[0])
            return {**s0, "wm": t, "incoming": round(s0["incoming"] * k), "routed": round(s0["incoming"] * k)}
        return self.samples[i] if i >= 0 and t - self.ts[i] < 60 else None

    def _wm(self, now: float) -> float:
        i = bisect_right(self.ts, now) - 1
        return self.samples[i]["wm"] if i >= 0 else now

    def _series(self) -> dict:
        """(namespace, metric, dimensions) -> the sample's value, for every metric this account publishes."""
        alb = {}
        for lb, share in ALBS.values():
            def req(s, k=share):
                return s["incoming"] * k
            alb |= {("AWS/ApplicationELB", "RequestCount", (("LoadBalancer", lb),)): lambda s, r=req: round(r(s)),
                    ("AWS/ApplicationELB", "HTTPCode_Target_2XX_Count", (("LoadBalancer", lb),)):
                        lambda s, r=req: round(r(s) * 0.9962),
                    ("AWS/ApplicationELB", "HTTPCode_Target_4XX_Count", (("LoadBalancer", lb),)):
                        lambda s, r=req: round(r(s) * 0.0035),
                    ("AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", (("LoadBalancer", lb),)):
                        lambda s, r=req: round(r(s) * 0.0003),
                    ("AWS/ApplicationELB", "TargetResponseTime", (("LoadBalancer", lb),)): lambda s: 0.084}
        cells = {("Vantleaf/Streamroute", "CellLagSeconds", (("Cell", f"lc-{i + 1:02d}"),)):
                 lambda s, i=i: round(s["cells"][i], 1) for i in range(8)}

        def freshness(s):
            return round(3.2 + max(s["cells"]), 1)
        return {
            ("Vantleaf/Canary", "LedgerE2ESuccess", ()): lambda s: s["canary"],
            ("Vantleaf/Canary", "CheckoutE2ESuccess", ()): lambda s: 1.0,
            ("Vantleaf/Streamroute", "RecordsRouted", ()): lambda s: s["routed"],
            ("Vantleaf/Streamroute", "RouteErrors", ()): lambda s: s["errors"],
            ("Vantleaf/Streamroute", "ShardMapValid", ()): lambda s: s["valid"],
            ("Vantleaf/Streamroute", "PeerCountKnown", ()): lambda s: s["peers"],
            ("Vantleaf/Streamroute", "EjectedHosts", ()): lambda s: s["ejected"],
            ("Vantleaf/Streamroute", "CellLagSeconds", ()): lambda s: round(max(s["cells"]), 1), **cells,
            ("CWAgent", "mem_used_percent", (ASG_DIM,)): lambda s: s["mem"],
            ("Vantleaf/Cellmap", "BootstrapConcurrent", ()): lambda s: s["boot"][0],
            ("Vantleaf/Cellmap", "Throttled429", ()): lambda s: s["boot"][3],
            ("Vantleaf/Cellmap", "LoadShedActive", ()): lambda s: s["boot"][2],
            ("AWS/Kinesis", "IncomingRecords", (STREAM,)): lambda s: s["incoming"],
            ("AWS/Kinesis", "IncomingBytes", (STREAM,)): lambda s: s["incoming"] * 612,
            ("AWS/Kinesis", "GetRecords.IteratorAgeMilliseconds", (STREAM,)): lambda s: round(max(s["cells"]) * 1000),
            ("AWS/Kinesis", "ReadProvisionedThroughputExceeded", (STREAM,)): lambda s: 0.0,
            ("AWS/ECS", "CPUUtilization", (("ClusterName", "vl-prod-core"), ("ServiceName", "ledger-writer"))):
                lambda s: round(min(92.0, 6.0 + 35.0 * s["routed"] / 60 / 1850), 1),
            ("Vantleaf/Payments", "PaymentVolumePerMin", ()): lambda s: round(s["incoming"] * 0.9),
            ("Vantleaf/Payments", "AuthSuccessRate", ()): lambda s: 0.978,
            ("Vantleaf/Payments", "LedgerFreshnessP99Seconds", ()): freshness,
            ("Vantleaf/Payments", "WebhookDeliveryLagP99", ()): lambda s: round(1.3 * freshness(s), 1), **alb}

    def value(self, ns: str, metric: str, dims: dict, t: float, now: float) -> float | None:
        """The datapoint of the minute starting at `t`, as published by `now`. Vantleaf/Payments (bizmetrics) publishes
        a minute only once the event-time watermark across all 8 cells has passed it."""
        get = self.series.get((ns, metric, tuple(sorted((dims or {}).items()))))
        s = self._sample(t)
        if get is None or s is None or t + 60 > now or ns == "Vantleaf/Payments" and t + 60 > self._wm(now):
            return None
        return float(get(s))

    def aggregate(self, ns, metric, dims, start, end, period, stats, now) -> list[tuple[float, dict]]:
        start, end = _s(start), min(_s(end), now)
        period = max(60, int(period or 60))
        out, t = [], start - start % 60
        while t < end and len(out) < 1440:
            vals = [v for x in range(int(t), int(min(t + period, end)), 60)
                    if (v := self.value(ns, metric, dims, x, now)) is not None]
            if vals:
                out.append((t, {st: _stat(vals, st) for st in stats}))
            t += period
        return out

    # ---- alarms ----
    def start(self, t: float) -> None:
        """Each alarm's history up to `t`: created at seeding, OK, then the transition `alarm_history_at_start` names
        (the times the #sre alarm posts give), else the one its own evaluation over the presimulation makes."""
        self.refresh_defs()
        for name, d in self.defs.items():
            state, at = (self.cfg.get("alarm_history_at_start") or HISTORY).get(name) or self._derived(d, t)
            since = parse_time(at, self.m.w.start_t).timestamp() if isinstance(at, str) else at or self.created + 60
            self.history.append(self._hist(name, self.created, "ConfigurationUpdate", f'Alarm "{name}" created'))
            self._move(name, "OK", self._eval(d, self.created + 60, "OK"), self.created + 60, None)
            if state and state != "OK":
                self._move(name, state, self._eval(d, since, state), since, "OK")
        self.started = True
        self.evaluate(t)

    def _derived(self, d: dict, t: float) -> list:
        """The state the alarm's evaluation gives at `t` and the presimulation minute it has held since."""
        state, since = self._eval(d, t)["state"], None
        for ts in reversed(self.ts[:bisect_right(self.ts, t)]):
            if self._eval(d, ts)["state"] != state:
                break
            since = ts
        return [state, None if since == self.ts[0] else since]

    def refresh_defs(self) -> None:
        """Alarm definitions as the emulator holds them, so an agent's PutMetricAlarm or DeleteAlarms takes effect."""
        try:
            out = self.m.w.aws.call("cloudwatch", "DescribeAlarms", {"MaxRecords": 100}, principal="awsbench:world",
                                    world_made=True)
        except AwsError:
            out = {}
        if "MetricAlarms" in (out or {}):
            defs = {a["AlarmName"]: a for a in out["MetricAlarms"]}
        else:
            defs = self.defs or self.seed_defs
        for name in set(self.defs) - set(defs):
            self.alarms.pop(name, None)
        changed = [n for n, d in defs.items() if n in self.defs and self._key(d) != self._key(self.defs[n])]
        self.defs = {n: dict(d) for n, d in defs.items()}
        for n in changed:
            self.history.append(self._hist(n, self.m.now, "ConfigurationUpdate", f'Alarm "{n}" updated'))

    @staticmethod
    def _key(d: dict) -> tuple:
        return tuple(str(d.get(k)) for k in ("Namespace", "MetricName", "Dimensions", "Statistic", "Period",
                                             "EvaluationPeriods", "DatapointsToAlarm", "Threshold",
                                             "ComparisonOperator", "TreatMissingData"))

    def _eval(self, d: dict, ts: float, force: str | None = None) -> dict:
        """CloudWatch's evaluation at `ts` over the last EvaluationPeriods periods, missing data per TreatMissingData."""
        period, n = int(d.get("Period", 60)), int(d.get("EvaluationPeriods", 1))
        need, stat = int(d.get("DatapointsToAlarm") or n), d.get("Statistic") or (d.get("ExtendedStatistic") or "Average")
        dims = {x["Name"]: x["Value"] for x in d.get("Dimensions") or []}
        pts = []
        for k in range(n):
            a = ts - ts % 60 - (k + 1) * period
            vals = [v for x in range(int(a), int(a + period), 60)
                    if (v := self.value(d["Namespace"], d["MetricName"], dims, x, ts)) is not None]
            pts.append((a, _stat(vals, stat) if vals else None, len(vals)))
        op, word = CMP.get(d.get("ComparisonOperator"), CMP["GreaterThanThreshold"])
        thr, treat = float(d.get("Threshold", 0)), d.get("TreatMissingData") or "missing"
        have = [(a, v, c) for a, v, c in pts if v is not None]
        bad = sum(op(float(v), thr) for _, v, _ in have) + (len(pts) - len(have)) * (treat == "breaching")
        state = ("ALARM" if bad >= need else "OK" if have or treat in ("breaching", "notBreaching") else
                 None if treat == "ignore" else "INSUFFICIENT_DATA")
        state = force or state
        if not have and state != "INSUFFICIENT_DATA":
            kind = "Breaching" if treat == "breaching" else "NonBreaching"
            reason = (f"Threshold Crossed: no datapoints were received for {n} period{'s' * (n > 1)} and {n} missing "
                      f"datapoint{'s were' if n > 1 else ' was'} treated as [{kind}].")
        elif state == "INSUFFICIENT_DATA":
            reason = f"Insufficient Data: {n} datapoint{'s were' if n > 1 else ' was'} unknown."
        else:
            listed = ", ".join(f"{_fmt(v)} ({datetime.fromtimestamp(a, UTC):%d/%m/%y %H:%M:%S})" for a, v, _ in have)
            m = need if state == "ALARM" else 1
            reason = (f"Threshold Crossed: {len(have)} out of the last {n} datapoints [{listed}] "
                      f"{'were' if state == 'ALARM' else 'were not'} {word} the threshold ({_fmt(thr)}) "
                      f"(minimum {m} datapoint{'s' * (m > 1)} for {'OK -> ALARM' if state == 'ALARM' else 'ALARM -> OK'}"
                      " transition).")
        data = {"version": "1.0", "queryDate": _z(ts), "startDate": _z(pts[-1][0]), "statistic": stat,
                "period": period, "recentDatapoints": [v for _, v, _ in reversed(have)], "threshold": thr,
                "evaluatedDatapoints": [{"timestamp": _z(a), **({"sampleCount": float(c), "value": v} if v is not None
                                                                else {})} for a, v, c in pts]}
        return {"state": state, "reason": reason, "data": json.dumps(data, separators=(",", ":"))}

    def evaluate(self, ts: float) -> None:
        for name, d in self.defs.items():
            ev = self._eval(d, ts)
            cur = self.alarms.get(name)
            if ev["state"] and (cur is None or ev["state"] != cur["state"]):
                self._move(name, ev["state"], ev, ts, cur and cur["state"])
        row = {n: a["state"] for n, a in sorted(self.alarms.items())}
        if not self.timeline or self.timeline[-1][1] != row:
            self.timeline.append([ts, row])

    def _move(self, name: str, state: str, ev: dict, ts: float, old: str | None) -> None:
        prev = self.alarms.get(name) or {"state": "INSUFFICIENT_DATA", "reason": "Unchecked: Initial alarm creation"}
        old = old or prev["state"]
        self.alarms[name] = {"state": state, "since": ts, "reason": ev["reason"], "data": ev["data"]}
        data = {"version": "1.0", "oldState": {"stateValue": old, "stateReason": prev["reason"]},
                "newState": {"stateValue": state, "stateReason": ev["reason"],
                             **({"stateReasonData": json.loads(ev["data"])} if ev.get("data") else {})}}
        self.history.append(self._hist(name, ts, "StateUpdate", f"Alarm updated from {old} to {state}", data))

    @staticmethod
    def _hist(name: str, ts: float, kind: str, summary: str, data: dict | None = None) -> dict:
        return {"AlarmName": name, "AlarmType": "MetricAlarm", "Timestamp": datetime.fromtimestamp(ts, UTC),
                "HistoryItemType": kind, "HistorySummary": summary,
                "HistoryData": json.dumps(data or {"version": "1.0", "type": kind}, separators=(",", ":"))}

    def set_state(self, p: dict, now: float) -> dict:
        name = p.get("AlarmName")
        if name not in self.defs:
            raise AwsError("ResourceNotFound", f"Alarm {name} not found", 404)
        ev = {"reason": p.get("StateReason", ""), "data": p.get("StateReasonData") or "{}"}
        self._move(name, p["StateValue"], ev, now, (self.alarms.get(name) or {}).get("state"))
        return {}

    def describe_alarms(self, p: dict, now: float) -> dict:
        if p.get("AlarmTypes") and "MetricAlarm" not in p["AlarmTypes"]:
            return {"MetricAlarms": [], "CompositeAlarms": []}
        want, prefix = set(p.get("AlarmNames") or []), p.get("AlarmNamePrefix") or ""
        out = []
        for name, d in sorted(self.defs.items()):
            a = self.alarms.get(name) or {"state": "INSUFFICIENT_DATA", "since": self.created,
                                          "reason": "Unchecked: Initial alarm creation", "data": None}
            if (want and name not in want or not name.startswith(prefix) or p.get("StateValue") not in
                    (None, a["state"]) or p.get("ActionPrefix") and not any(
                        x.startswith(p["ActionPrefix"]) for x in d.get("AlarmActions") or [])):
                continue
            keep = {k: d[k] for k in ("AlarmName", "AlarmDescription", "OKActions", "AlarmActions",
                                      "InsufficientDataActions", "MetricName", "Namespace", "Statistic",
                                      "ExtendedStatistic", "Dimensions", "Period", "EvaluationPeriods",
                                      "DatapointsToAlarm", "Threshold", "ComparisonOperator", "TreatMissingData",
                                      "Unit") if d.get(k) is not None}
            since = datetime.fromtimestamp(a["since"], UTC)
            out.append({**keep, "AlarmArn": f"arn:aws:cloudwatch:us-east-1:{ACCOUNT}:alarm:{name}",
                        "AlarmConfigurationUpdatedTimestamp": datetime.fromtimestamp(self.created, UTC),
                        "ActionsEnabled": d.get("ActionsEnabled", True), "OKActions": d.get("OKActions") or [],
                        "InsufficientDataActions": d.get("InsufficientDataActions") or [],
                        "Dimensions": d.get("Dimensions") or [], "StateValue": a["state"],
                        "StateReason": a["reason"], **({"StateReasonData": a["data"]} if a["data"] else {}),
                        "StateUpdatedTimestamp": since, "StateTransitionedTimestamp": since})
        return {"MetricAlarms": out, "CompositeAlarms": []}

    def alarm_history(self, p: dict, now: float) -> dict:
        lo, hi = _s(p["StartDate"]) if p.get("StartDate") else 0, _s(p["EndDate"]) if p.get("EndDate") else now
        rows = [h for h in self.history if (not p.get("AlarmName") or h["AlarmName"] == p["AlarmName"])
                and (not p.get("HistoryItemType") or h["HistoryItemType"] == p["HistoryItemType"])
                and lo <= h["Timestamp"].timestamp() <= hi]
        rows.sort(key=lambda h: h["Timestamp"], reverse=p.get("ScanBy") != "TimestampAscending")
        return {"AlarmHistoryItems": rows[:int(p.get("MaxRecords") or 100)]}

    # ---- cloudwatch ----
    def cloudwatch(self, op: str, p: dict, now: float):
        if op == "GetMetricData":
            res = []
            for q in p.get("MetricDataQueries", []):
                ms = q.get("MetricStat") or {}
                met = ms.get("Metric") or {}
                if not met:
                    raise AwsError("ValidationError", "Only MetricStat queries are supported in this environment; "
                                                      "math expressions and Metrics Insights are not.", 400)
                stat = ms.get("Stat") or "Average"
                dims = {d["Name"]: d["Value"] for d in met.get("Dimensions", [])}
                pts = self.aggregate(met.get("Namespace"), met.get("MetricName"), dims, p["StartTime"], p["EndTime"],
                                     ms.get("Period", 60), [stat], now)
                pts = pts if p.get("ScanBy") == "TimestampAscending" else pts[::-1]
                res.append({"Id": q.get("Id"), "Label": q.get("Label") or met.get("MetricName"),
                            "Timestamps": [datetime.fromtimestamp(t, UTC) for t, _ in pts],
                            "Values": [v[stat] for _, v in pts], "StatusCode": "Complete"})
            return {"MetricDataResults": res, "Messages": []}
        if op == "GetMetricStatistics":
            dims = {d["Name"]: d["Value"] for d in p.get("Dimensions", [])}
            ext = p.get("ExtendedStatistics") or []
            stats = p.get("Statistics") or ([] if ext else ["Average"])
            pts = self.aggregate(p["Namespace"], p["MetricName"], dims, p["StartTime"], p["EndTime"],
                                 p.get("Period", 60), stats + ext, now)
            return {"Label": p["MetricName"], "Datapoints": [
                {"Timestamp": datetime.fromtimestamp(t, UTC), **{s: v[s] for s in stats},
                 **({"ExtendedStatistics": {e: v[e] for e in ext}} if ext else {}), "Unit": "None"} for t, v in pts]}
        if op == "ListMetrics":
            want = {d["Name"]: d.get("Value") for d in p.get("Dimensions") or []}
            return {"Metrics": [{"Namespace": ns, "MetricName": mn, "Dimensions": [{"Name": k, "Value": v}
                                                                                   for k, v in dims]}
                                for ns, mn, dims in self.series
                                if p.get("Namespace") in (None, ns) and p.get("MetricName") in (None, mn)
                                and all(k in dict(dims) and v in (None, dict(dims)[k]) for k, v in want.items())]}
        if op == "DescribeAlarms":
            return self.describe_alarms(p, now)
        if op == "DescribeAlarmHistory":
            return self.alarm_history(p, now)
        if op == "SetAlarmState":
            return self.set_state(p, now)
        return None

    # ---- logs ----
    def lines(self, group: str, a: float, b: float) -> list[tuple[float, str, str]]:
        """(t, stream, message) of `group` in [a, b], from the fleet's own log and the minute samples."""
        f = self.m.fleet
        out = [(t, STREAMS.get(g, host), line) for t, g, host, line in f.logs if g == group and a <= t <= b]
        if group == "/vl/prod/ledger-writer":
            out.append((self.m.at(LW_START[0]), STREAMS[group], LW_START[1]))
        for i in range(bisect_left(self.ts, a - 59), bisect_right(self.ts, b)):
            t, s = self.ts[i], self.samples[i]
            n = int(t // 60) % 1_000_000
            if group == "/vl/prod/streamroute":
                out += [(t + 7 + 40 * unit("t03-logs", h, n), h, NO_MAP) for h in s["broken"]]
                out += [(t + 3 + 50 * unit("t03-logs", h, n), h, RENEW_SLOW) for h in s["stale"]]
            elif group == "/vl/prod/streamroute-gc" and s["gc"]:
                h, u = s["gc"], unit("t03-gc", s["gc"], n)
                used = 2250 + int(350 * u)
                out.append((t + 13, h, (f"[gc] GC({8000 + n % 9000}) Pause Young (Normal) (G1 Evacuation Pause) "
                                        f"{used}M->{used - 120 - int(60 * u)}M(6144M) {15 + 10 * u:.1f}ms")))
            elif group == "/vl/prod/ledger-writer":
                out.append((t + 59, STREAMS[group], "INFO idle: no records from streamroute in 60s" if s["routed"] < 1
                            else f"INFO committed {s['routed']} ledger entries in 60s (errors 0, p99 write 38ms)"))
            elif group == "/vl/prod/bizmetrics":
                behind = [f"lc-{c + 1:02d}" for c, lag in enumerate(s["cells"]) if lag > 120]
                wm = datetime.fromtimestamp(s["wm"], self.m.tz).strftime("%H:%M:%S")
                out.append((t + 20, STREAMS[group], f"WARN watermark stalled at {wm} (cells {behind[0]}..{behind[-1]} "
                            "behind); holding windows" if behind else
                            f"INFO window {datetime.fromtimestamp(t - 60, self.m.tz):%H:%M} closed "
                            f"(watermark {wm}, all cells)"))
            elif group == "/vl/prod/cellmap" and (s["boot"][0] or s["boot"][1] or s["boot"][2] or n % 5 == 0):
                out.append((t + 1, STREAMS[group], "ERROR BootstrapSnapshot load-shed active: answering 503"
                            if s["boot"][2] else f"INFO BootstrapSnapshot in_flight={s['boot'][0]}/12 "
                                                 f"retrying={s['boot'][1]}"))
            elif group == "/aws/lambda/ledger-e2e-canary":
                out.append((t + 2, STREAMS[group], f"INFO event evt_canary_{n} visible in ledger after "
                                                   f"{2 + 3 * unit('t03-canary', n):.1f}s" if s["canary"] else
                            f"ERROR event evt_canary_{n} not visible in ledger after 120s"))
        return sorted(x for x in out if a <= x[0] <= b)

    def _known(self, g) -> str:
        g = _group(g)
        if g not in self.groups:
            raise AwsError("ResourceNotFoundException", "The specified log group does not exist.", 400)
        return g

    def logs(self, op: str, p: dict, now: float):
        if op in ("FilterLogEvents", "GetLogEvents"):
            g = self._known(p.get("logGroupName") or p.get("logGroupIdentifier"))
            a = p["startTime"] / 1000 if p.get("startTime") else now - 3600
            b = min(p["endTime"] / 1000 if p.get("endTime") else now, now)
            names = set(p.get("logStreamNames") or ([p["logStreamName"]] if p.get("logStreamName") else []))
            pre, ok = p.get("logStreamNamePrefix") or "", _matcher(p.get("filterPattern"))
            evs = [x for x in self.lines(g, a, b) if (not names or x[1] in names) and x[1].startswith(pre) and ok(x[2])]
            tok = p.get("nextToken") or ""
            off = int(tok.rsplit("/", 1)[-1]) if tok.rsplit("/", 1)[-1].isdigit() else 0
            if op == "GetLogEvents" and not p.get("startFromHead") and not tok:
                off = max(0, len(evs) - min(PAGE, int(p.get("limit") or PAGE)))
            page = evs[off:off + min(PAGE, int(p.get("limit") or PAGE))]
            items = [{"logStreamName": st, "timestamp": _ms(t), "message": msg, "ingestionTime": _ms(t) + 380,
                      "eventId": f"{_ms(t)}{k:04d}"} for k, (t, st, msg) in enumerate(page, off)]
            if op == "GetLogEvents":
                return {"events": [{k: e[k] for k in ("timestamp", "message", "ingestionTime")} for e in items],
                        "nextForwardToken": f"f/{off + len(page)}", "nextBackwardToken": f"b/{max(0, off - PAGE)}"}
            more = off + len(page) < len(evs)
            return {"events": items, "searchedLogStreams": [], **({"nextToken": f"t/{off + len(page)}"} if more else {})}
        if op == "DescribeLogStreams":
            g = self._known(p.get("logGroupName") or p.get("logGroupIdentifier"))
            names = sorted(self.m.fleet.hosts) if g in ("/vl/prod/streamroute", "/vl/prod/streamroute-gc") else [
                STREAMS[g]]
            pre = p.get("logStreamNamePrefix") or ""
            first = self.ts[0] if self.ts else now
            rows = [{"logStreamName": n, "creationTime": _ms(self.created),
                     "arn": f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:{g}:log-stream:{n}",
                     "firstEventTimestamp": _ms(first), "lastEventTimestamp": _ms(now - 30),
                     "lastIngestionTime": _ms(now - 29), "storedBytes": 0} for n in names if n.startswith(pre)]
            return {"logStreams": rows[:int(p.get("limit") or 50)]}
        if op == "StartQuery":
            groups = [self._known(g) for g in p.get("logGroupNames") or p.get("logGroupIdentifiers") or
                      ([p["logGroupName"]] if p.get("logGroupName") else [])]
            if not groups:
                raise AwsError("InvalidParameterException", "Specify logGroupName, logGroupNames or "
                                                            "logGroupIdentifiers.", 400)
            qid = f"{len(self.queries) + 1:08x}-5b1e-4c2d-9f3a-7d6e0c1b2a{len(self.queries):02x}"
            self.queries[qid] = self.run_query(p, groups, parse_query(p.get("queryString", "")), now)
            return {"queryId": qid}
        if op == "GetQueryResults":
            return self.queries.get(p.get("queryId"))
        return None

    def run_query(self, p: dict, groups: list[str], plan: dict, now: float) -> dict:
        a, b = float(p.get("startTime") or now - 3600), min(float(p.get("endTime") or now), now)
        scanned = [(g, x) for g in groups for x in self.lines(g, a, b)]
        rows = [(g, x) for g, x in scanned if all(f(x[2]) for f in plan["filters"])]
        limit = min(plan["limit"] or 10000, int(p.get("limit") or 10000))

        def stamp(t):
            return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        if plan["stats"]:
            st, buckets = plan["stats"], {}
            for g, (t, _, _) in rows:
                k = (int(t) // st["bin_s"] * st["bin_s"] if st["bin_s"] else 0, g if "@log" in st["by"] else "")
                buckets[k] = buckets.get(k, 0) + 1
            results = [[{"field": f, "value": g if f == "@log" else stamp(bk)} for f in st["by"]]
                       + [{"field": st["name"], "value": str(n)}] for (bk, g), n in sorted(buckets.items())][:limit]
        else:
            rows.sort(key=lambda r: r[1][0], reverse=plan["sort"] != "asc")
            fields = plan["fields"] or ["@timestamp", "@message"] + (["@log"] if len(groups) > 1 else [])
            val = {"@timestamp": lambda g, x: stamp(x[0]), "@message": lambda g, x: x[2],
                   "@log": lambda g, x: f"{ACCOUNT}:{g}", "@logStream": lambda g, x: x[1]}
            results = [[{"field": f, "value": val[f](g, x)} for f in fields] for g, x in rows[:limit]]
        return {"status": "Complete", "results": results,
                "statistics": {"recordsMatched": float(len(rows)), "recordsScanned": float(len(scanned)),
                               "bytesScanned": float(sum(len(x[2]) + 60 for _, x in scanned))}}

    # ---- lambda and the lease table ----
    def canary_fn(self) -> dict:
        return {"FunctionName": CANARY_FN, "FunctionArn": f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:{CANARY_FN}",
                "Runtime": "python3.12", "Role": f"arn:aws:iam::{ACCOUNT}:role/vl-ledger-canary", "Handler":
                "canary.handler", "CodeSize": 4211, "Description": "Every minute: one synthetic payment event on "
                "vl-payment-events, then checks the ledger for it (Vantleaf/Canary LedgerE2ESuccess)", "Timeout": 150,
                "MemorySize": 256, "LastModified": _z(self.created), "Version": "$LATEST", "State": "Active",
                "LastUpdateStatus": "Successful", "PackageType": "Zip", "Architectures": ["arm64"],
                "Environment": {"Variables": {"STREAM": "vl-payment-events", "VISIBLE_WITHIN_S": "120"}},
                "LoggingConfig": {"LogFormat": "Text", "LogGroup": "/aws/lambda/ledger-e2e-canary"}}

    def lambda_(self, op: str, p: dict):
        name = str(p.get("FunctionName") or "").rsplit(":", 1)[-1]
        if op == "ListFunctions":
            try:
                out = self.m.w.aws.call("lambda", "ListFunctions", p, principal="awsbench:world", world_made=True)
            except AwsError:
                out = {}
            return {**out, "Functions": [*(out or {}).get("Functions", []), self.canary_fn()]}
        if name != CANARY_FN:
            return None
        if op == "GetFunctionConfiguration":
            return self.canary_fn()
        if op == "GetFunction":
            return {"Configuration": self.canary_fn(), "Code": {"RepositoryType": "S3", "Location":
                    f"https://awslambda-us-east-1-tasks.s3.us-east-1.amazonaws.com/snapshots/{ACCOUNT}/{CANARY_FN}"},
                    "Tags": {"service": "ledger-canary", "owner": "sre"}}
        return None

    def sync_leases(self, now: float) -> None:
        """streamroute-leases as the hosts keep it: the world's owner of each shard, its counter renewed every 10 s
        while the owner routes and its checkpoint at what the shard has routed. Not logged: KCL's own writes."""
        flow = self.m.flow
        per = flow.p["shards"] // flow.p["cells"]
        items = [{"PutRequest": {"Item": {
            "leaseKey": {"S": f"shardId-{s:012d}"}, "leaseOwner": {"S": flow.owner[s]},
            "leaseCounter": {"N": str(self.counter[s] if self.counter else 18000)},
            "checkpoint": {"S": str(49650 * 10 ** 51 + s * 10 ** 46 + int(flow.cons[s] * flow.w[s]) * 10 ** 20)},
            "checkpointSubSequenceNumber": {"N": "0"}, "ownerSwitchesSinceCheckpoint": {"N": "0"},
            "cell": {"S": f"lc-{s // per + 1:02d}"}}}} for s in range(flow.p["shards"])]
        try:
            for i in range(0, len(items), 25):
                self.m.w.aws.call("dynamodb", "BatchWriteItem", {"RequestItems": {LEASES: items[i:i + 25]}},
                                  principal="awsbench:world", world_made=True, log_call=False)
        except (AwsError, TypeError):
            return

    # ---- dispatch and export ----
    def serve(self, service: str, op: str, p: dict, now: float):
        service = service.lower()
        if service in ("cloudwatch", "monitoring"):
            return self.cloudwatch(op, p, now)
        if service == "logs":
            return self.logs(op, p, now)
        if service == "lambda":
            return self.lambda_(op, p)
        if service == "dynamodb" and op in ("Scan", "Query", "GetItem", "BatchGetItem") and LEASES in str(p):
            self.sync_leases(now)
        return None

    def export(self, iso) -> dict:
        return {"alarm_state_timeline": [[iso(t), row] for t, row in self.timeline]}
