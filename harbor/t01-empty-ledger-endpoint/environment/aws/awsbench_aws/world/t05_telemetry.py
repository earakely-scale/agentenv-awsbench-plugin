"""T05 telemetry served by the world (docs/T05-build.md §1.5): CloudWatch metrics from per-minute samples of the
mechanics (a sample at minute M is the state at M and reads as the datapoint of [M, M+60)), alarm states evaluated
each minute over the definitions the emulator holds, and CloudWatch Logs generated from state (vaultstore index and
placement, capctl-audit, billing-aux, payments-gateway, receipt-renderer). Logs Insights runs the shared parser; JSON
log lines' fields are discovered as Logs Insights discovers them."""

import json
from bisect import bisect_left, bisect_right
from datetime import UTC, datetime

from ..account import ACCOUNT
from ..errors import AwsError
from .t05_vaultstore import COLDSTART, CRASHLOOP, FULL, POOLS, READ, SERVING, STATE_CODE
from .telemetry import _group, _stat, _xplan, run_insights
from .util import unit

IDX, PLC, AUDIT, BILL = ("/vl/platform/vaultstore/index", "/vl/platform/vaultstore/placement",
                         "/vl/platform/vaultstore/capctl-audit", "/vl/platform/vaultstore/billing-aux")
PG, RCPT = "/vl/prod/payments-gateway", "/aws/lambda/receipt-renderer"
GROUPS = (IDX, PLC, AUDIT, BILL, PG, RCPT)
LONG_TAIL = ("mch_28561", "mch_30477", "mch_41893", "mch_13350", "mch_11872", "mch_37755", "mch_16233", "mch_22764")
PAGE = 1000
CMP = {"GreaterThanThreshold": float.__gt__, "GreaterThanOrEqualToThreshold": float.__ge__,
       "LessThanThreshold": float.__lt__, "LessThanOrEqualToThreshold": float.__le__}
GC_DIPS = ("2026-11-02T03:14", "2026-11-04T11:41", "2026-11-06T22:05", "2026-11-09T16:27", "2026-11-11T04:52",
           "2026-11-13T13:18", "2026-11-15T19:40", "2026-11-17T08:09", "2026-11-19T02:33")


def _ms(t: float) -> int:
    return int(t * 1000)


def _s(v) -> float:
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e11 else float(v)
    if isinstance(v, datetime):
        return v.timestamp()
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()


def _match(pattern: str | None):
    if not pattern:
        return lambda s: True
    terms = [t.strip('"') for t in pattern.split() if t.strip('"')]
    return lambda s: all(t in s for t in terms)


class Telemetry:
    def __init__(self, mech, cfg: dict):
        self.m = mech
        self.ts: list[float] = []
        self.samples: list[dict] = []
        self.history: list[dict] = []
        self.states: dict[str, dict] = {}
        self.queries: dict[str, dict] = {}
        self.timeline: list = []
        self.gc_dips = [datetime.fromisoformat(d + ":00-05:00").timestamp() for d in GC_DIPS]
        self._cache: tuple = (None, [])

    # ---- samples ----
    def record(self, t: float) -> None:
        m, vs, im = self.m, self.m.vs, self.m.impact
        done, cur, keys = vs.partitions()
        s = {"auth": im.auth_error_rate(t), "idx": vs.idx["state"], "pl": vs.pl["state"],
             "live": {p: vs.live(p) for p in POOLS}, "done": done, "cur": cur, "keys": keys,
             "phase": vs.phase(), "restarts": sum(1 for x in vs.restart_counts if t - 60 < x <= t),
             "backlog": round(im.backlog), "failing": im.receipts_failing(), "threeds": im.threeds(t),
             "batch": vs.batch["state"], "tp": vs.throughput(), "proj": vs.projected_finish(t),
             "stale": im.effective("/vl/prod/payments-gateway/config-cache/serve-stale", t) == "true",
             "snapshot": im.effective("/vl/prod/payments-gateway/config-source", t) == "s3-snapshot"}
        self.ts.append(t)
        self.samples.append(s)
        self.evaluate(t)

    def sample(self, t: float) -> dict | None:
        i = bisect_right(self.ts, t) - 1
        return self.samples[i] if i >= 0 and t - self.ts[i] < 60 else None

    def baseline(self, ns: str, metric: str, dims: dict, t: float) -> float | None:
        if (ns, metric) == ("Vantleaf/Vaultstore", "IndexAvailability"):
            return 0.0 if any(d <= t < d + 120 + 120 * unit("t05-gc", d) for d in self.gc_dips) else 100.0
        base = {("Vantleaf/Payments", "AuthErrorRate"): 0.1, ("Vantleaf/Payments", "AuthRequests"): 2600.0,
                ("Vantleaf/Vaultstore", "PlacementRestarts"): 0.0, ("Vantleaf/Vaultstore", "ColdstartProgress"): 0.0,
                ("Vantleaf/Receipts", "BacklogDepth"): 0.0, ("Vantleaf/Receipts", "RenderErrorRate"): 0.0,
                ("Vantleaf/Checkout", "ThreeDSFailureRate"): 0.37, ("AWS/SQS", "ApproximateNumberOfMessagesVisible"):
                0.0, ("AWS/Lambda", "Errors"): 0.0, ("AWS/Lambda", "Invocations"): 1900.0,
                ("Vantleaf/Billing", "BatchState"): 1.0, ("Vantleaf/Billing", "BatchThroughput"): 0.29,
                ("Vantleaf/Dashboard", "Downloads5xx"): 0.0, ("Vantleaf/Vaultstore", "RequestErrorRate"): 0.0}
        if (ns, metric) == ("Vantleaf/Vaultstore", "SubsystemState"):
            return 3.0
        if (ns, metric) == ("Vantleaf/Vaultstore", "LiveMembers"):
            return float(len(POOLS.get(dims.get("Pool"), ("", 0, []))[2]))
        if (ns, metric) == ("Vantleaf/Checkout", "ThreeDSFailureRate") and t >= self.m.at("10:24:40"):
            return 0.0
        return base.get((ns, metric))

    def value(self, ns: str, metric: str, dims: dict, t: float) -> float | None:
        s = self.sample(t)
        if s is None:
            return self.baseline(ns, metric, dims, t) if t < (self.ts[0] if self.ts else self.m.now) else None
        q = (ns, metric)
        if q == ("Vantleaf/Payments", "AuthErrorRate"):
            return s["auth"]
        if q == ("Vantleaf/Payments", "AuthRequests"):
            return 2600.0
        if q == ("Vantleaf/Vaultstore", "LiveMembers"):
            return float(s["live"].get(dims.get("Pool"), 0)) if dims.get("Pool") in POOLS else None
        if q == ("Vantleaf/Vaultstore", "SubsystemState"):
            sub = dims.get("Subsystem")
            return float(STATE_CODE[s["idx"] if sub == "index" else s["pl"]]) if sub in ("index", "placement") else None
        if q == ("Vantleaf/Vaultstore", "ColdstartProgress"):
            return float(s["done"]) if dims.get("Subsystem") == "index" else None
        if q == ("Vantleaf/Vaultstore", "IndexAvailability"):
            return 100.0 if s["idx"] in (READ, FULL) else 0.0
        if q == ("Vantleaf/Vaultstore", "PlacementRestarts"):
            return float(s["restarts"])
        if q == ("Vantleaf/Vaultstore", "RequestErrorRate"):
            op = dims.get("Op")
            return {"get": 0.0 if s["idx"] in (READ, FULL) else 100.0, "list": 0.0 if s["idx"] in (READ, FULL) else
                    100.0, "put": 100.0 if s["failing"] else 0.0}.get(op)
        if q == ("Vantleaf/Receipts", "BacklogDepth") or q == ("AWS/SQS", "ApproximateNumberOfMessagesVisible"):
            return float(s["backlog"]) if q[0] != "AWS/SQS" or dims.get("QueueName") == "vl-receipt-jobs" else (
                0.0 if dims.get("QueueName") == "vl-receipt-jobs-dlq" else None)
        if q == ("Vantleaf/Receipts", "RenderErrorRate"):
            return 100.0 if s["failing"] else 0.0
        if q == ("AWS/Lambda", "Errors"):
            return 1900.0 if s["failing"] else 0.0
        if q == ("AWS/Lambda", "Invocations"):
            return 1900.0 + (7100.0 if not s["failing"] and s["backlog"] > 0 else 0.0)
        if q == ("Vantleaf/Checkout", "ThreeDSFailureRate"):
            return s["threeds"] if dims.get("merchant") == "mch_11872" else None
        if q == ("Vantleaf/Billing", "BatchState"):
            return {"PAUSED": 0.0, "RUNNING": 1.0, "ABORTED": 2.0, "DONE": 3.0}[s["batch"]]
        if q == ("Vantleaf/Billing", "BatchThroughput"):
            return 0.0 if s["batch"] != "RUNNING" else round(s["tp"], 3)
        if q == ("Vantleaf/Billing", "ProjectedFinishMinutes"):
            return None if s["proj"] is None else float(round((s["proj"] - t // 60 * 60) / 60))
        if q == ("Vantleaf/Dashboard", "Downloads5xx"):
            return 140.0 if s["idx"] not in (READ, FULL) else 0.0
        return None

    def series(self) -> list[tuple[str, str, tuple]]:
        out = [("Vantleaf/Payments", "AuthErrorRate", ()), ("Vantleaf/Payments", "AuthRequests", ()),
               ("Vantleaf/Vaultstore", "IndexAvailability", ()), ("Vantleaf/Vaultstore", "PlacementRestarts", ()),
               ("Vantleaf/Vaultstore", "ColdstartProgress", (("Subsystem", "index"),)),
               ("Vantleaf/Receipts", "BacklogDepth", ()), ("Vantleaf/Receipts", "RenderErrorRate", ()),
               ("Vantleaf/Checkout", "ThreeDSFailureRate", (("merchant", "mch_11872"),)),
               ("Vantleaf/Billing", "BatchState", (("Batch", "billing-usage"),)),
               ("Vantleaf/Billing", "BatchThroughput", (("Batch", "billing-usage"),)),
               ("Vantleaf/Billing", "ProjectedFinishMinutes", (("Batch", "billing-usage"),)),
               ("Vantleaf/Dashboard", "Downloads5xx", ()),
               ("AWS/SQS", "ApproximateNumberOfMessagesVisible", (("QueueName", "vl-receipt-jobs"),)),
               ("AWS/SQS", "ApproximateNumberOfMessagesVisible", (("QueueName", "vl-receipt-jobs-dlq"),)),
               ("AWS/Lambda", "Errors", (("FunctionName", "receipt-renderer"),)),
               ("AWS/Lambda", "Invocations", (("FunctionName", "receipt-renderer"),))]
        out += [("Vantleaf/Vaultstore", "LiveMembers", (("Pool", p),)) for p in POOLS]
        out += [("Vantleaf/Vaultstore", "SubsystemState", (("Subsystem", s),)) for s in ("index", "placement")]
        out += [("Vantleaf/Vaultstore", "RequestErrorRate", (("Op", o),)) for o in ("get", "put", "list")]
        return out

    def aggregate(self, ns, metric, dims, start, end, period, stats, now) -> list[tuple[float, dict]]:
        a, b = _s(start), min(_s(end), now // 60 * 60)
        period = max(60, int(period or 60))
        out, t = [], a // period * period
        while t < b:
            vals = [v for k in range(0, period, 60) if t + k < b and (v := self.value(ns, metric, dims, t + k))
                    is not None]
            if vals:
                out.append((t, {s: _stat(vals, s) for s in stats if not s.startswith("p")} |
                            {s: sorted(vals)[min(len(vals) - 1, int(len(vals) * float(s[1:]) / 100))]
                             for s in stats if s.startswith("p")}))
            t += period
        return out

    # ---- alarms ----
    def defs(self) -> list[dict]:
        try:
            out = self.m.w.aws.call("cloudwatch", "DescribeAlarms", {"MaxRecords": 100}, principal="awsbench:world",
                                    world_made=True)
        except (AwsError, TypeError, AttributeError):
            return []
        return [d for d in (out or {}).get("MetricAlarms") or []]

    def points(self, d: dict, t: float) -> list[tuple[float, float | None]]:
        """The datapoints an evaluation at `t` reads: one per period, oldest first."""
        dims = {x["Name"]: x["Value"] for x in d.get("Dimensions") or []}
        n = int(d.get("EvaluationPeriods") or 1)
        return [(tt, self.value(d.get("Namespace"), d.get("MetricName"), dims, tt))
                for tt in (t // 60 * 60 - 60 * k for k in range(n, 0, -1))]

    def eval_one(self, d: dict, t: float) -> tuple[str, str]:
        n, need = int(d.get("EvaluationPeriods") or 1), int(d.get("DatapointsToAlarm") or d.get("EvaluationPeriods") or 1)
        pts = [v for _, v in self.points(d, t)]
        known = [v for v in pts if v is not None]
        cmp = CMP.get(d.get("ComparisonOperator"), float.__gt__)
        thr = float(d.get("Threshold") or 0)
        if not known:
            return "INSUFFICIENT_DATA", "Insufficient Data: no datapoints were received."
        breach = sum(1 for v in known if cmp(float(v), thr))
        shown = ", ".join(f"{v:g}" for v in known[-5:])
        if breach >= need:
            return "ALARM", (f"Threshold Crossed: {breach} out of the last {n} datapoints [{shown}] were "
                             f"{'greater' if 'Greater' in d.get('ComparisonOperator', '') else 'less'} than the threshold "
                             f"({thr:g}).")
        return "OK", (f"Threshold Crossed: {len(known) - breach} out of the last {n} datapoints [{shown}] were not "
                      f"{'greater' if 'Greater' in d.get('ComparisonOperator', '') else 'less'} than the threshold "
                      f"({thr:g}).")

    def evaluate(self, t: float) -> None:
        if int(t) % 60:
            return
        row = {}
        for d in self._defs_cache(t):
            name = d["AlarmName"]
            state, reason = self.eval_one(d, t)
            cur = self.states.get(name)
            if cur is None or cur["state"] != state:
                old = cur["state"] if cur else "INSUFFICIENT_DATA"
                self.states[name] = {"state": state, "reason": reason, "since": t}
                if cur is not None or t > self.m.at("10:00"):
                    self.history.append({"AlarmName": name, "AlarmType": "MetricAlarm",
                                         "Timestamp": datetime.fromtimestamp(t, UTC), "HistoryItemType": "StateUpdate",
                                         "HistorySummary": f"Alarm updated from {old} to {state}"})
            else:
                cur["reason"] = reason
            self.states[name].update(eval_t=t, pts=self.points(d, t), stat=d.get("Statistic") or "Average",
                                     period=int(d.get("Period") or 60), thr=float(d.get("Threshold") or 0))
            row[name] = state
        if t >= self.m.t_start:
            self.timeline.append([t, row])

    def virtual(self, x):
        """An emulator stamp as virtual time (seeded rows read seeded_at), through the server's wall-time map."""
        times = getattr(self.m.w.aws, "times", None)
        v = times.virtual(x.timestamp()) if times is not None and isinstance(x, datetime) else None
        return v.astimezone(UTC) if v else x

    @staticmethod
    def reason_data(st: dict) -> str | None:
        if "eval_t" not in st:
            return None
        f = "%Y-%m-%dT%H:%M:%S.000+0000"
        pts = st["pts"]
        known = [(tt, v) for tt, v in pts if v is not None]
        out = {"version": "1.0", "queryDate": datetime.fromtimestamp(st["eval_t"], UTC).strftime(f),
               "startDate": datetime.fromtimestamp(pts[0][0] if pts else st["eval_t"], UTC).strftime(f),
               "statistic": st["stat"], "period": st["period"], "recentDatapoints": [round(v, 4) for _, v in known],
               "threshold": st["thr"]}
        if known:
            out["evaluatedDatapoints"] = [{"timestamp": datetime.fromtimestamp(tt, UTC).strftime(f), "value": round(v, 4)}
                                          for tt, v in reversed(known)]
        return json.dumps(out)

    def _defs_cache(self, t: float) -> list[dict]:
        if self._cache[0] is None or t - self._cache[0] >= 300 or self.m.defs_dirty:
            self._cache = (t, self.defs())
            self.m.defs_dirty = False
        return self._cache[1]

    def seed_history(self) -> None:
        """PLAT-1290's flaps of vaultstore-index-availability (GC pauses, Nov 1-19), as alarm history."""
        for d in self.gc_dips:
            for k, (old, new) in enumerate((("OK", "ALARM"), ("ALARM", "OK"))):
                self.history.append({"AlarmName": "vaultstore-index-availability", "AlarmType": "MetricAlarm",
                                     "Timestamp": datetime.fromtimestamp(d + 300 + 240 * k, UTC),
                                     "HistoryItemType": "StateUpdate",
                                     "HistorySummary": f"Alarm updated from {old} to {new}"})

    def cloudwatch(self, op: str, p: dict, now: float):
        if op == "GetMetricData":
            res = []
            for q in p.get("MetricDataQueries") or []:
                ms = q.get("MetricStat") or {}
                met = ms.get("Metric") or {}
                if not met:
                    raise AwsError("ValidationError", "Only MetricStat queries are supported in this environment; "
                                                      "math expressions and Metrics Insights are not.", 400)
                stat = ms.get("Stat") or "Average"
                dims = {x["Name"]: x["Value"] for x in met.get("Dimensions") or []}
                pts = self.aggregate(met.get("Namespace"), met.get("MetricName"), dims, p["StartTime"], p["EndTime"],
                                     ms.get("Period", 60), [stat], now)
                pts = pts if p.get("ScanBy") == "TimestampAscending" else pts[::-1]
                res.append({"Id": q.get("Id"), "Label": q.get("Label") or met.get("MetricName"),
                            "Timestamps": [datetime.fromtimestamp(t, UTC) for t, _ in pts],
                            "Values": [v[stat] for _, v in pts], "StatusCode": "Complete"})
            return {"MetricDataResults": res, "Messages": []}
        if op == "GetMetricStatistics":
            dims = {x["Name"]: x["Value"] for x in p.get("Dimensions") or []}
            stats = (p.get("Statistics") or []) + (p.get("ExtendedStatistics") or []) or ["Average"]
            pts = self.aggregate(p["Namespace"], p["MetricName"], dims, p["StartTime"], p["EndTime"],
                                 p.get("Period", 60), stats, now)
            return {"Label": p["MetricName"], "Datapoints": [
                {"Timestamp": datetime.fromtimestamp(t, UTC), **{s: v[s] for s in stats if not s.startswith("p")},
                 **({"ExtendedStatistics": {s: v[s] for s in stats if s.startswith("p")}}
                    if any(s.startswith("p") for s in stats) else {}), "Unit": "None"} for t, v in pts]}
        if op == "ListMetrics":
            want = {x["Name"]: x.get("Value") for x in p.get("Dimensions") or []}
            return {"Metrics": [{"Namespace": ns, "MetricName": mn, "Dimensions": [{"Name": k, "Value": v}
                                                                                   for k, v in dims]}
                                for ns, mn, dims in self.series()
                                if p.get("Namespace") in (None, ns) and p.get("MetricName") in (None, mn)
                                and all(k in dict(dims) and v in (None, dict(dims)[k]) for k, v in want.items())]}
        if op == "DescribeAlarms":
            try:
                out = self.m.w.aws.call("cloudwatch", "DescribeAlarms", p, principal="awsbench:world",
                                        world_made=True)
            except (AwsError, TypeError, AttributeError):
                return None
            rows = []
            for d in (out or {}).get("MetricAlarms") or []:
                d = {k: self.virtual(v) if k.endswith("Timestamp") else v for k, v in d.items()}
                st = self.states.get(d["AlarmName"])
                if st:
                    since = datetime.fromtimestamp(st["since"], UTC)
                    d = {**d, "StateValue": st["state"], "StateReason": st["reason"], "StateUpdatedTimestamp": since,
                         "StateTransitionedTimestamp": since, "StateReasonData": self.reason_data(st)}
                else:
                    d.pop("StateReasonData", None)
                rows.append(d)
            if p.get("StateValue"):
                rows = [r for r in rows if r.get("StateValue") == p["StateValue"]]
            return {**(out or {}), "MetricAlarms": rows}
        if op == "DescribeAlarmHistory":
            lo = _s(p["StartDate"]) if p.get("StartDate") else 0
            hi = _s(p["EndDate"]) if p.get("EndDate") else now
            rows = [h for h in self.history if (not p.get("AlarmName") or h["AlarmName"] == p["AlarmName"])
                    and (not p.get("HistoryItemType") or h["HistoryItemType"] == p["HistoryItemType"])
                    and lo <= h["Timestamp"].timestamp() <= min(hi, now)]
            rows.sort(key=lambda h: h["Timestamp"], reverse=p.get("ScanBy") != "TimestampAscending")
            return {"AlarmHistoryItems": rows[:int(p.get("MaxRecords") or 100)]}
        return None

    # ---- logs ----
    def partition_rate(self, now: float, since: float | None = None) -> float:
        i = bisect_left(self.ts, max(now - 600, since or 0))
        if i >= len(self.samples):
            return 0.0
        done = self.m.vs.partitions()[0] if self.m.vs.idx.get("checked") else self.samples[-1]["done"]
        return max(0.0, (done - self.samples[i]["done"]) / max(1.0, (now - self.ts[i]) / 60))

    def placement_restarts(self, now: float, window: float) -> int:
        return sum(1 for x in self.m.vs.restart_counts if now - window < x <= now)

    def lines(self, group: str, a: float, b: float) -> list[tuple[float, str, str]]:
        vs = self.m.vs
        out = []
        if group == IDX:
            out += [(t, st, msg) for t, st, msg in vs.idx_log]
        elif group == PLC:
            out += [(t, st, msg) for t, st, msg in vs.pl_log]
        elif group == AUDIT:
            out += [(t, st, msg) for t, st, msg in vs.audit]
        elif group == BILL:
            out += [(t, st, msg) for t, st, msg in vs.billing_log]
        lo, hi = bisect_left(self.ts, a - 60), bisect_right(self.ts, b)
        for i in range(lo, hi):
            t, s = self.ts[i], self.samples[i]
            n = int(t // 60)
            if group == IDX and s["idx"] == COLDSTART and s["phase"] == "verify-partitions":
                total = 139.7e6 if s["cur"] == 311 else 0.6e6 + 0.8e6 * unit("t05-part", s["cur"])
                kv = s["keys"] if s["cur"] == 311 else total * unit("t05-kv", n)
                out.append((t + 0.5, "verify", json.dumps({
                    "level": "INFO", "logger": "verify-partitions",
                    "msg": f"verify-partitions progress {s['done']}/512 partition={s['cur']} keys_verified="
                           f"{kv / 1e6:.1f}M/{total / 1e6:.1f}M", "done": s["done"], "total": 512,
                    "partition": s["cur"], "keys_verified": int(kv), "keys_total": int(total)})))
            elif group == IDX and s["idx"] == COLDSTART and s["phase"] == "verify-manifests":
                out.append((t + 0.5, "verify", json.dumps({"level": "INFO", "logger": "verify-manifests",
                                                           "msg": "verify-manifests in progress"})))
            elif group == IDX and s["idx"] not in (COLDSTART, READ, FULL) and n % 5 == 0:
                out.append((t + 1, "guard", f"WARN guard: PROTECTIVE_HALT (live {s['live']['vs-blobindex']}/16); "
                                            "waiting for cold start"))
            elif group == PLC and s["pl"] not in (SERVING, CRASHLOOP, COLDSTART) and n % 5 == 0:
                out.append((t + 1, "guard", f"WARN guard: PROTECTIVE_HALT (live {s['live']['vs-blobplace']}/10; index "
                                            f"{s['idx']}); waiting for cold start"))
            elif group == BILL and n % 15 == 0 and s["batch"] in ("RUNNING", "PAUSED"):
                out.append((t + 2, "billing-usage", f"INFO billing-usage {s['batch']}: throughput={s['tp']:.2f}; "
                                                    f"members={s['live']['vs-billing-aux']}"))
            elif group == PG:
                out += self.pg_lines(t, s, n)
            elif group == RCPT and s["failing"]:
                for k in range(8):
                    m = LONG_TAIL[int(8 * unit("t05-rcpt-m", n, k))]
                    out.append((t + 7 * k + 3 * unit("t05-rcpt", n, k), "2026/11/20/[$LATEST]7e2c1a9f04d3b5e6",
                                f"ERROR PutObject vs://receipts/2026/11/{m}/rc_{n % 100000:05d}{k}.pdf 503 "
                                f"PlacementUnavailable; message returned to queue (receive_count="
                                f"{1 + int(9 * unit('t05-rc', n, k))})"))
        return sorted(x for x in out if a <= x[0] <= b)

    def pg_lines(self, t: float, s: dict, n: int) -> list:
        rate = s["auth"]
        if rate <= 0.15 and not s["stale"]:
            return []
        out = []
        count = int(round(2600 * rate / 100))
        for k in range(count):
            m = LONG_TAIL[int(4 * unit("t05-pg-m", n, k))]
            ts = t + 59 * k / max(1, count)
            if s["idx"] in (READ, FULL):
                continue
            if s["stale"] or s["snapshot"]:
                if k < 6:
                    out.append((ts, "pg", json.dumps({"level": "WARN", "event": "merchant_config.fetch", "merchant": m,
                                                      "code": "PG-W-CFG-STALE", "err": "IndexUnavailable",
                                                      "msg": "served stale merchant config (soft-expired)" if s["stale"]
                                                      else "merchant config from s3-snapshot"})))
                continue
            out.append((ts, "pg", json.dumps({"level": "ERROR", "event": "merchant_config.fetch", "merchant": m,
                                              "key": f"vs://merchant-config/{m}/v{412 + k % 7}.json", "status": 503,
                                              "code": "PG-E-CFG-503", "err": "IndexUnavailable",
                                              "lkg": "miss(top500=false)", "serve_stale": False,
                                              "msg": "merchant_config.fetch status=503 code=IndexUnavailable -> auth "
                                                     "declined (PG-E-CFG-503)"})))
        return out

    def node_lines(self, h, a: float, b: float) -> list[tuple[float, str]]:
        out = []
        for i in range(bisect_left(self.ts, a), bisect_right(self.ts, b)):
            t, s = self.ts[i], self.samples[i]
            if h.sub == "index" and s["idx"] == COLDSTART and s["phase"] == "verify-partitions" and s["cur"] == 311:
                out.append((t, f"vaultstore-index: verify-partitions: partition=311 keys_verified="
                               f"{s['keys'] / 1e6:.1f}M/139.7M (+15.5M/min)"))
            elif h.sub == "index" and s["idx"] == COLDSTART and int(t // 60) % 5 == 0:
                out.append((t, f"vaultstore-index: coldstart: {s['phase']} {s['done']}/512"))
            elif h.sub == "index" and s["idx"] not in (COLDSTART, READ, FULL) and int(t // 60) % 5 == 0:
                out.append((t, f"vaultstore-index: guard: PROTECTIVE_HALT (live {s['live']['vs-blobindex']} < floor "
                               "16); waiting for cold start"))
            elif h.sub == "placement" and s["pl"] == CRASHLOOP and int(t // 60) % 3 == 0:
                out.append((t, "vaultstore-placement: allocation-map validation failed: index not SERVING_FULL; "
                               "exiting (systemd will restart)"))
        return out

    def logs(self, op: str, p: dict, now: float):
        g = _group(p.get("logGroupName") or p.get("logGroupIdentifier"))
        if op in ("FilterLogEvents", "GetLogEvents") and g in GROUPS:
            a = _s(p["startTime"]) if p.get("startTime") else now - 3600
            b = min(_s(p["endTime"]) if p.get("endTime") else now, now - 5)
            names = set(p.get("logStreamNames") or ([p["logStreamName"]] if p.get("logStreamName") else []))
            pre, ok = p.get("logStreamNamePrefix") or "", _match(p.get("filterPattern"))
            evs = [x for x in self.lines(g, a, b) if (not names or x[1] in names) and x[1].startswith(pre)
                   and ok(x[2])]
            tok = p.get("nextToken") or ""
            off = int(tok.rsplit("/", 1)[-1]) if tok.rsplit("/", 1)[-1].isdigit() else 0
            lim = min(PAGE, int(p.get("limit") or PAGE))
            if op == "GetLogEvents" and not p.get("startFromHead") and not tok:
                off = max(0, len(evs) - lim)
            page = evs[off:off + lim]
            items = [{"logStreamName": st, "timestamp": _ms(t), "message": msg, "ingestionTime": _ms(t) + 410,
                      "eventId": f"{_ms(t)}{k:05d}"} for k, (t, st, msg) in enumerate(page, off)]
            if op == "GetLogEvents":
                return {"events": [{k: e[k] for k in ("timestamp", "message", "ingestionTime")} for e in items],
                        "nextForwardToken": f"f/{off + len(page)}", "nextBackwardToken": f"b/{max(0, off - lim)}"}
            more = off + len(page) < len(evs)
            return {"events": items, "searchedLogStreams": [], **({"nextToken": f"t/{off + len(page)}"} if more else {})}
        if op == "StartQuery":
            groups = [_group(x) for x in p.get("logGroupNames") or p.get("logGroupIdentifiers") or
                      ([p["logGroupName"]] if p.get("logGroupName") else [])]
            if not groups or not all(x in GROUPS for x in groups):
                return None
            ops = _xplan(p.get("queryString", ""))
            qid = f"{len(self.queries) + 1:08x}-5c2e-4d7a-8b1f-3e9a0d6c2b{len(self.queries):02x}"
            a, b = float(p.get("startTime") or now - 3600), min(float(p.get("endTime") or now), now - 5)
            recs = []
            for grp in groups:
                for t, st, msg in self.lines(grp, a, b):
                    rec = {"@timestamp": _ms(t), "@message": msg, "@log": f"{ACCOUNT}:{grp}", "@logStream": st}
                    if msg.startswith("{"):
                        try:
                            rec.update({k: v for k, v in json.loads(msg).items() if not isinstance(v, (dict, list))})
                        except ValueError:
                            pass
                    elif (w := msg.split(" ", 1)[0]) in ("ERROR", "WARN", "INFO", "DEBUG"):
                        rec["level"] = w
                    recs.append(rec)
            limit = int(p.get("limit") or 1000)
            results, matched = run_insights(ops, recs, limit, ["@timestamp", "@message"] +
                                            (["@log"] if len(groups) > 1 else []))
            self.queries[qid] = {"status": "Complete", "results": results,
                                 "statistics": {"recordsMatched": float(matched), "recordsScanned": float(len(recs)),
                                                "bytesScanned": float(sum(len(r["@message"]) for r in recs))}}
            return {"queryId": qid}
        if op == "GetQueryResults" and p.get("queryId") in self.queries:
            return self.queries[p["queryId"]]
        return None

    def serve(self, service: str, op: str, p: dict, now: float):
        service = service.lower()
        if service in ("cloudwatch", "monitoring"):
            return self.cloudwatch(op, p, now)
        if service == "logs":
            return self.logs(op, p, now)
        return None

    def export(self, iso) -> dict:
        return {"alarm_state_timeline": [[iso(t), row] for t, row in self.timeline]}
