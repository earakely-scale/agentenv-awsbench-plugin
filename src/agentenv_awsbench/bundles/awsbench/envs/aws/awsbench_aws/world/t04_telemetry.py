"""T04 telemetry served by the world (docs/T04-build.md §1.6): payments failures per merchant and minute from the data
plane, Vantleaf/Payments and Vantleaf/Routemap metrics, the five engine-owned alarms (rules.py's cell-7 drift
included), and CloudWatch Logs for the routemap and payments groups. A minute `M`'s datapoint is published once the
minute is over. Log messages are JSON, and Logs Insights discovers their fields as it does in AWS."""

import json
from bisect import bisect_left, bisect_right
from datetime import UTC, datetime

from ..account import ACCOUNT
from ..errors import AwsError
from . import t04_data as D
from .telemetry import _group, _stat, _utc, _xplan, run_insights

CURVE = [("06:00", 900), ("13:00", 2900), ("16:00", 3600), ("23:59", 1400)]
PAGE = 1000
ALARMS = {  # name: (initial state, since HH:MM or None)
    "payments-route-no-healthy-cell-high": ("ALARM", "13:06"), "routemap-api-5xx-high": ("ALARM", "09:14"),
    "cell-7-utilization-high": ("ALARM", "07:52"), "cell-7-auth-latency-high": ("OK", "06:00"),
    "checkout-cache-evictions": ("ALARM", "12:40")}
STREAMS = {"/ecs/payments-gateway": "ecs/payments-gateway/5d1c8e7a20b4", "/routemap/prod/audit": "rm-admin-01",
           "/aws/lambda/routemap-nightly-backup": "2026/11/21/[$LATEST]3e9b0c7d5a1f4e2b8c6d9a0f1e2b3c4d"}
BASE_MS, PEAK_MS, GRACE_S, RAMP_S, RECOVER_S = 420.0, 900.0, 3600.0, 7200.0, 600.0


def _ms(t: float) -> int:
    return int(t * 1000)


def _s(t) -> float:
    return _utc(t).timestamp()


def _matcher(pattern: str | None):
    terms = [t for t in (pattern or "").replace("{", " ").replace("}", " ").split() if t]
    need = [t.strip('"') for t in terms if t[0] not in "?-"]
    anyof = [t[1:].strip('"') for t in terms if t[0] == "?"]
    nope = [t[1:].strip('"') for t in terms if t[0] == "-"]
    return lambda s: (all(n in s for n in need) and (not anyof or any(a in s for a in anyof))
                      and not any(n in s for n in nope))


class Telemetry:
    def __init__(self, m, cfg: dict):
        self.m, self.cfg = m, cfg
        self.stored: dict[str, list] = {}
        self.minutes: list[float] = []
        self.fails: list[dict] = []
        self.counters: dict[tuple, dict] = {}
        self.alarms: dict[str, dict] = {}
        self.history: list[dict] = []
        self.timeline: list = []
        self.queries: dict[str, dict] = {}
        self.cell7: float = BASE_MS
        self.lat: dict[float, float] = {}
        self.paused_since: float | None = None
        self.recover_from = None
        self.hannah = False
        self.fail_series: list = []
        seed = m.w.scenario.get("aws_seed") or []
        self.groups = [r["params"]["logGroupName"] for r in seed
                       if r.get("service") == "logs" and r.get("operation") == "CreateLogGroup"]
        self.defs = {r["params"]["AlarmName"]: r["params"] for r in seed
                     if r.get("service") == "cloudwatch" and r.get("operation") == "PutMetricAlarm"}
        self.created = D.ts("2026-09-01T11:00:00-04:00")

    # ---- recording ----
    def add(self, t: float, group: str, stream: str, msg: str) -> None:
        rows = self.stored.setdefault(group, [])
        if rows and t < rows[-1][0]:
            rows.insert(bisect_right([r[0] for r in rows], t), (t, stream, msg))
        else:
            rows.append((t, stream, msg))

    def count(self, metric: str, dims: dict, t: float, n: float = 1) -> None:
        ns = "Vantleaf/Payments" if metric in ("DuplicateCaptures",) else "Vantleaf/Routemap"
        key = (ns, metric, tuple(sorted(dims.items())))
        m = t - t % 60
        self.counters.setdefault(key, {})[m] = self.counters.setdefault(key, {}).get(m, 0) + n

    def api(self, t: float, op: str, status: int) -> None:
        self.count("ApiRequests", {"Op": op, "Status": "5xx" if status >= 500 else "2xx"}, t)

    def demand(self, t: float) -> float:
        loc = datetime.fromtimestamp(t, D.ET)
        x = loc.hour * 60 + loc.minute + loc.second / 60
        pts = [(int(a[:2]) * 60 + int(a[3:]), v) for a, v in CURVE]
        if x <= pts[0][0]:
            return pts[0][1]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if x <= x1:
                return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
        return pts[-1][1]

    def rate(self, mid: str, t: float) -> float:
        if mid in D.KEY_MERCHANTS:
            return float(D.KEY_MERCHANTS[mid])
        return (0.6 + 0.8 * D.u("T04-rate", mid)) * self.demand(t) / 2900

    def n_fail(self, mid: str, m: float) -> int:
        r = self.rate(mid, m)
        return int(r) + (1 if D.u("T04-n", mid, int(m)) < r - int(r) else 0)

    def start(self, t_start: float) -> None:
        """History before 13:12: the 29 rewritten merchants failing from their rewrite, the routemap logs the seed
        writes left, the reconciler's drift summaries and routemap-api's 500s on purged routes."""
        m, sets = self.m, self.m.sets
        rewrites = {mid: t for mid, _, t in sets["rewrites"]}
        a = D.ts("2026-11-21T07:00:00-05:00")
        for minute in range(int(a), int(t_start), 60):
            broken = {mid: self.n_fail(mid, minute) for mid, t in rewrites.items() if t < minute}
            self.minutes.append(float(minute))
            self.fails.append({k: v for k, v in broken.items() if v})
        self._seed_logs(t_start, rewrites)
        for name, (state, since) in ALARMS.items():
            if name in self.defs:
                self.alarms[name] = {"state": state, "since": m.at(since),
                                     "reason": "Threshold Crossed: datapoints were above the threshold" if
                                     state == "ALARM" else "Threshold Crossed: datapoints were not above the threshold"}
        self.timeline.append([t_start, {n: a["state"] for n, a in sorted(self.alarms.items())}])

    def _seed_logs(self, t_start: float, rewrites: dict) -> None:
        m, sets, hist = self.m, self.m.sets, self.m.hist
        purge0 = D.ts("2026-11-20T19:42:08.114-05:00")
        audit = [(purge0, {"actor": f"arn:aws:sts::{ACCOUNT}:assumed-role/AWSReservedSSO_PlatformEng_3f1a9c0e7b2d4a6f/"
                                    "ravi.menon@vantleaf.com", "host": "ravi-mbp",
                           "cmd": "rmctl maintenance purge-orphans --registry-snapshot "
                                  "s3://vl-staging-merchant-registry/snapshots/2026-11-20T06.json --apply",
                           "env_resolved": f"prod (caller account {ACCOUNT}; --env not given)", "job_id": D.PURGE_JOB,
                           "phase": "start", "candidates": 371, "publish_mode": "SUPPRESS"})]
        for i, n in enumerate([50] * 7 + [21]):
            audit.append((purge0 + 33.394 + 66.13 * i if i < 7 else D.ts("2026-11-20T19:49:51.377-05:00"),
                          {"job_id": D.PURGE_JOB, "phase": "batch", "batch": f"{i + 1} of 8", "tombstoned": n}))
        audit.append((D.ts("2026-11-20T19:49:51.337-05:00") + 0.1, {
            "job_id": D.PURGE_JOB, "phase": "complete", "tombstoned": 371,
            "job_record": f"s3://vl-routemap-ops-{ACCOUNT}/maintenance/{D.PURGE_JOB}/job.json"}))
        role = f"arn:aws:sts::{ACCOUNT}:assumed-role/vl-rm-admin-role"
        for t, who, cmd in [("2026-11-20T17:20:15.020-05:00", "ravi.menon", "rmctl route set mch_41207 --cells "
                             "cell-ca-2 --pool pool-ca-1 --fallback cell-ca-1 --residency CA --ticket PLAT-2336"),
                            ("2026-11-21T10:15:44.020-05:00", "grace.liu", "rmctl route pin mch_11872 --cell cell-9 "
                             "--ticket PLAT-2346"),
                            ("2026-11-21T11:20:41.030-05:00", "marco.ferri", "rmctl limits set mch_30988 --daily 2500 "
                             "--per-txn 500 --reason 'PAY-1911 card-testing pattern'")]:
            audit.append((D.ts(t), {"actor": f"{role}/{who}", "host": "rm-admin-01", "cmd": cmd,
                                    "env_resolved": "prod (--env prod)"}))
        routine = ["rmctl route describe {mid}", "rmctl workflows status", "rmctl dataplane status {mid}"]
        t = D.ts("2026-11-16T09:00:00-05:00")
        k = 0
        while t < t_start:
            who = ["grace.liu", "ravi.menon", "kwame.asante", "tomas.ilyin"][k % 4]
            mid = sets["established"][D.h("T04-audit", k) % len(sets["established"])]
            if not (D.ts("2026-11-20T19:00:00-05:00") < t < D.ts("2026-11-21T09:30:00-05:00")):
                audit.append((t, {"actor": f"{role}/{who}", "host": "rm-admin-01",
                                  "cmd": routine[k % 3].format(mid=mid), "env_resolved": "prod (auto)"}))
            k += 1
            t += 3600 * (1.2 + 0.8 * D.u("T04-audit-gap", k))
        for t, row in sorted(audit, key=lambda x: x[0]):
            self.add(t, "/routemap/prod/audit", "rm-admin-01", json.dumps(row, separators=(",", ":")))
        m.rmctl.audit_rows = [(t, row) for t, row in sorted(audit, key=lambda x: x[0])]
        for mid, (t, *_rest) in {**D.FRIDAY_ITEMS, **D.SATURDAY_ITEMS}.items():
            ts = D.ts(t)
            self.add(ts, "/ecs/routemap-api", "ecs/routemap-api/4c2e9a7f10b3", json.dumps(
                {"level": "INFO", "op": "CreateRoute", "merchant": mid, "caller": "merchant-onboarding", "status": 200}))
            self.add(ts + 0.4, "/ecs/merchant-onboarding", "ecs/merchant-onboarding/0b7e2c41f9a3", json.dumps(
                {"level": "INFO", "merchant": mid, "msg": "route created"}))
        for mid, t in (("mch_37755", "2026-11-20T19:31:12.640-05:00"), ("mch_12904", "2026-11-21T10:02:31-05:00"),
                       ("mch_26631", "2026-11-21T10:48:09-05:00"), ("mch_38140", "2026-11-21T11:37:52-05:00")):
            self.add(D.ts(t), "/ecs/routemap-api", "ecs/routemap-api/4c2e9a7f10b3", json.dumps(
                {"level": "INFO", "op": "UpdateRoute", "merchant": mid, "caller": "merchant-dashboard", "status": 200,
                 "changed": ["settlement_profile.payout_schedule"]}))
        purged = sets["purged"]
        for i, mid in enumerate(purged):
            ts = purge0 + 0.4 + i * (463.2 / 371)
            self.add(ts, "/ecs/routemap-publisher", "publisher-1", json.dumps(
                {"level": "DEBUG", "msg": "publish suppressed (publish_mode=SUPPRESS)", "merchant": mid}))
        for mid, writer, t in sets["rewrites"]:
            v = next(it["route_version"] for tt, it, _ in hist.rows[(mid, D.ROUTE)] if tt == t)
            wf = writer.split("-")[1]
            self.add(t, f"/ecs/{writer}", f"ecs/{wf}/{D.h(wf, int(t - t % 600)) % 16 ** 12:012x}", json.dumps(
                {"level": "WARN", "svc": writer, "merchant": mid, "msg": "route_spec missing; initialised from defaults",
                 "new_version": v, "published": True}))
            self.add(t + 0.5, "/ecs/routemap-publisher", "publisher-1", json.dumps(
                {"merchant": mid, "version": v, "cells": 0, "result": "ACCEPTED"}))
        tick = D.ts("2026-11-20T19:00:00-05:00")
        while tick < t_start:
            loc = datetime.fromtimestamp(tick, D.ET)
            if loc.minute % 10 == 0:
                st = f"ecs/scaler/{D.h('scaler', int(tick)) % 16 ** 12:012x}"
                n = sum(1 for _, w, t in sets["rewrites"] if w == "routemap-scaler" and tick <= t < tick + 600)
                self.add(tick + 5 + 3, "/ecs/routemap-scaler", st, json.dumps(
                    {"level": "INFO", "svc": "routemap-scaler", "msg": "run complete", "scope": "all",
                     "touched": 70 if tick >= D.ts("2026-11-21T13:00:00-05:00") else 10, "defaults_written": n}))
            if loc.minute == 35:
                st = f"ecs/rebalancer/{D.h('rebalancer', int(tick)) % 16 ** 12:012x}"
                n = sum(1 for _, w, t in sets["rewrites"] if w == "routemap-rebalancer" and tick <= t < tick + 600)
                self.add(tick + 9, "/ecs/routemap-rebalancer", st, json.dumps(
                    {"level": "INFO", "svc": "routemap-rebalancer", "msg": "run complete", "scope": "all",
                     "touched": 12 + n, "defaults_written": n}))
            if loc.minute in (5, 35) and tick >= D.ts("2026-11-20T20:05:00-05:00"):
                snap_t = tick + 2
                tomb = 371 - sum(1 for _, _, t in sets["rewrites"] if t < snap_t)
                self.add(snap_t, "/ecs/routemap-reconciler", f"ecs/reconciler/{D.h('rec', int(tick)) % 16 ** 12:012x}",
                         json.dumps({"level": "WARN", "svc": "routemap-reconciler", "msg": "drift summary",
                                     "state_purged_but_dataplane_active": tomb, "state_empty_cells": 371 - tomb,
                                     "action": "none (reconcile-only)"}))
            tick += 300
        # merchant-dashboard saves on purged routes since the purge: 500 under v2.31.0's strict model
        t, k = D.ts("2026-11-20T19:55:00-05:00"), 0
        latent0 = [x for x in purged if x not in set(D.NEVER_TOUCHED)]
        while t < t_start:
            mid = latent0[D.h(D.SAVES_SEED, "pre", k) % len(latent0)]
            if mid not in rewrites or rewrites[mid] > t:
                self.add(t, "/ecs/routemap-api", "ecs/routemap-api/4c2e9a7f10b3", json.dumps(
                    {"level": "ERROR", "op": "UpdateRoute" if k % 3 else "DescribeRoute", "merchant": mid,
                     "caller": "merchant-dashboard", "status": 500, "error": "InternalError: route reconcile failed",
                     "trace": "File \"routemap/model.py\", line 211, in from_item: spec = item['route_spec'] "
                              "KeyError: 'route_spec'"}))
            loc = datetime.fromtimestamp(t, D.ET)
            k += 1
            t += 180 if 8 <= loc.hour < 22 or loc.day == 21 and loc.hour >= 7 else 900
        for day in (16, 17, 18, 19, 20, 21):
            t = D.ts(f"2026-11-{day}T03:00:00-05:00")
            if t < t_start:
                name = f"routemap-state-nightly-{datetime.fromtimestamp(t, UTC):%Y%m%dT%H%MZ}"
                self.add(t + 1.2, "/aws/lambda/routemap-nightly-backup", STREAMS["/aws/lambda/routemap-nightly-backup"],
                         f"INFO CreateBackup routemap-state -> {name} (status CREATING)")

    # ---- each minute ----
    def minute(self, minute: float) -> list[dict]:
        """The minute ending at `minute + 60` is recorded at its start tick: failures from the data plane now."""
        if self.minutes and minute <= self.minutes[-1]:
            return []
        m = self.m
        broken = {}
        for mid in sorted(m.purged | set(D.MUST_EXCLUDE) | {"mch_44419", "mch_44420"}):
            cfg = m.dp.get(mid)
            if cfg is None and (mid, D.ROUTE) not in m.live:
                continue
            if m.broken(cfg) and (n := self.n_fail(mid, minute)):
                broken[mid] = n
        self.minutes.append(minute)
        self.fails.append(broken)
        dup = sum(max(1, round(0.02 * self.rate(mid, minute))) for mid, until in m.dup.items() if minute < until)
        if dup:
            self.count("DuplicateCaptures", {}, minute, dup)
        for mid, n in sorted(broken.items()):
            item = m.live.get((mid, D.ROUTE)) or {}
            cfg = m.dp.get(mid) or {}
            self.add(minute + 59, "/ecs/payments-gateway", STREAMS["/ecs/payments-gateway"], json.dumps(
                {"level": "WARN", "event": "auth_rejected", "code": "ROUTE_NO_HEALTHY_CELL", "merchant": mid,
                 "route_version": cfg.get("version", item.get("route_version")), "cells": cfg.get("cells") or [],
                 "fallback": cfg.get("fallback_cell") or "cell-drain", "count_1m": n}, separators=(",", ":")))
        self._cell7(minute)
        out = self._evaluate(minute + 60)
        self.fail_series.append([minute, sum(broken.values())])
        return out

    def _cell7(self, t: float) -> None:
        """rules.py: AuthLatencyP99{cell-7} drifts 420 -> 900 ms over 2 h once the scaler has been paused 60 min."""
        m = self.m
        paused = not m.flag("/routemap/prod/workflows/scaler/enabled")
        full = not paused and m.ssm.get("/routemap/prod/workflows/scaler/scope", "all") == "all"
        if paused:
            self.paused_since = self.paused_since or t
            held = t - self.paused_since
            if held > GRACE_S:
                self.cell7 = BASE_MS + (PEAK_MS - BASE_MS) * min(1.0, (held - GRACE_S) / RAMP_S)
            self.recover_from = None
        else:
            self.paused_since = None
            if self.cell7 > BASE_MS and full:
                self.recover_from = self.recover_from or (t, self.cell7)
                t0, v0 = self.recover_from
                self.cell7 = v0 - (v0 - BASE_MS) * min(1.0, (t - t0) / RECOVER_S)
        self.lat[t] = round(self.cell7, 1)

    # ---- metrics ----
    def fail_at(self, t: float) -> dict | None:
        i = bisect_right(self.minutes, t) - 1
        return self.fails[i] if i >= 0 and self.minutes[i] == t - t % 60 else None

    def value(self, ns: str, metric: str, dims: dict, t: float, now: float) -> float | None:
        t = t - t % 60
        if t + 60 > now or t < D.ts("2026-11-14T00:00:00-05:00"):
            return None
        if ns == "Vantleaf/Payments":
            if metric == "PaymentAttempts" and not dims:
                return round(self.demand(t) * (0.98 + 0.04 * D.u("T04-att", int(t))))
            if metric == "PaymentFailures":
                reason = dims.get("Reason")
                if reason == "ROUTE_NO_HEALTHY_CELL":
                    f = self.fail_at(t)
                    return float(sum(f.values())) if f is not None else 0.0
                if reason == "ISSUER_DECLINE":
                    extra = 6 * self.rate("mch_15110", t) if t >= self.m.at("11:00") else 0
                    return round(self.demand(t) * (0.02 + 0.02 * D.u("T04-dec", int(t))) + extra)
                return None
            if metric == "AuthLatencyP99" and "Cell" in dims:
                if dims["Cell"] == "cell-7":
                    return self.lat.get(t, BASE_MS)
                return 260.0 + 60 * D.u("T04-lat", dims["Cell"])
            if metric == "CellUtilization" and "Cell" in dims:
                return 0.82 if dims["Cell"] == "cell-7" else round(0.4 + 0.2 * D.u("T04-util", dims["Cell"]), 2)
            if metric == "DuplicateCaptures" and not dims:
                return float(self.counters.get(("Vantleaf/Payments", "DuplicateCaptures", ()), {}).get(t, 0))
        if ns == "Vantleaf/Routemap":
            key = (ns, metric, tuple(sorted(dims.items())))
            n = float(self.counters.get(key, {}).get(t, 0))
            if metric == "ApiRequests" and dims.get("Status") == "5xx" and t < self.m.t_start:
                return 6.0 if t >= D.ts("2026-11-21T09:00:00-05:00") else 0.0
            if metric == "ApiRequests" and dims.get("Status") == "2xx":
                n += 24 if dims.get("Op") == "UpdateRoute" else 38 if dims.get("Op") == "DescribeRoute" else 0
            if metric == "ApiRequests" and dims.get("Status") == "5xx" and t >= self.m.t_start:
                tomb = len(self.m.latent())
                n += round(6.0 * tomb / 342, 1) if self.m.strict() else 0
            return n
        return None

    def series(self) -> list:
        cells = [f"cell-{i}" for i in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)]
        out = [("Vantleaf/Payments", "PaymentAttempts", ()),
               ("Vantleaf/Payments", "PaymentFailures", (("Reason", "ROUTE_NO_HEALTHY_CELL"),)),
               ("Vantleaf/Payments", "PaymentFailures", (("Reason", "ISSUER_DECLINE"),)),
               ("Vantleaf/Payments", "DuplicateCaptures", ())]
        out += [("Vantleaf/Payments", m, (("Cell", c),)) for m in ("AuthLatencyP99", "CellUtilization") for c in cells]
        out += [("Vantleaf/Routemap", "ApiRequests", (("Op", o), ("Status", s))) for o in ("UpdateRoute", "DescribeRoute",
                                                                                           "CreateRoute")
                for s in ("2xx", "5xx")]
        out += [("Vantleaf/Routemap", "WorkflowWrites", (("Workflow", w),)) for w in ("scaler", "rebalancer")]
        return out

    def aggregate(self, ns, metric, dims, start, end, period, stats, now) -> list:
        a, b = _s(start), min(_s(end), now)
        period = max(60, int(period or 60))
        out, t = [], a - a % period
        while t < b:
            vals = [v for x in range(int(max(t, a - a % 60)), int(t + period), 60)
                    if x < b and (v := self.value(ns, metric, dims, x, now)) is not None]
            if vals:
                out.append((t, {s: _stat(vals, s) for s in stats}))
            t += period
        return out

    # ---- alarms ----
    def _evaluate(self, now: float) -> list[dict]:
        out = []
        for name in self.defs:
            state = self._state(name, now)
            cur = self.alarms.get(name)
            if state and cur and state != cur["state"]:
                self.history.append({"AlarmName": name, "AlarmType": "MetricAlarm",
                                     "Timestamp": datetime.fromtimestamp(now, UTC), "HistoryItemType": "StateUpdate",
                                     "HistorySummary": f"Alarm updated from {cur['state']} to {state}",
                                     "HistoryData": json.dumps({"version": "1.0", "oldState": {"stateValue":
                                                                cur["state"]}, "newState": {"stateValue": state}})})
                self.alarms[name] = {"state": state, "since": now, "reason": f"Threshold Crossed: alarm moved from "
                                     f"{cur['state']} to {state}"}
                if state == "ALARM" and name in ("payments-route-no-healthy-cell-high", "cell-7-auth-latency-high"):
                    p1 = name.startswith("payments")
                    res = self.m.w.ws_apply("pager.page_as", {
                        "from_user": "pagerbot", "to": "sre-primary", "urgency": "high" if p1 else "low",
                        "summary": f"{'P1' if p1 else 'P2'} {name} triggered"}, self.m.dt(now))
                    out += res.get("deliveries", [])
        row = {n: a["state"] for n, a in sorted(self.alarms.items())}
        if not self.timeline or self.timeline[-1][1] != row:
            self.timeline.append([now, row])
        return out

    def _state(self, name: str, now: float) -> str | None:
        if name == "payments-route-no-healthy-cell-high":
            end = now - now % 300
            pts = [sum(sum((self.fail_at(x) or {}).values()) for x in range(int(e - 300), int(e), 60))
                   for e in (end, end - 300, end - 600)]
            return "ALARM" if sum(p > 150 for p in pts) >= 2 else "OK"
        if name == "routemap-api-5xx-high":
            end = now - now % 300
            vals = []
            for e in (end, end - 300, end - 600):
                bad = sum(self.value("Vantleaf/Routemap", "ApiRequests", {"Op": o, "Status": "5xx"}, x, now) or 0
                          for o in ("UpdateRoute", "DescribeRoute") for x in range(int(e - 300), int(e), 60))
                good = sum(self.value("Vantleaf/Routemap", "ApiRequests", {"Op": o, "Status": "2xx"}, x, now) or 0
                           for o in ("UpdateRoute", "DescribeRoute") for x in range(int(e - 300), int(e), 60))
                vals.append(bad / max(1.0, bad + good))
            return "ALARM" if all(v > 0.05 for v in vals) or (self.alarms.get(name, {}).get("state") == "ALARM"
                                                              and vals[0] > 0.04) else "OK"
        if name == "cell-7-auth-latency-high":
            recent = [self.lat.get(x) for x in range(int(now - 660), int(now - 60), 60)]
            return "ALARM" if recent and all(v is not None and v > 800 for v in recent) else "OK"
        return None

    def describe_alarms(self, p: dict, now: float) -> dict:
        want, prefix = set(p.get("AlarmNames") or []), p.get("AlarmNamePrefix") or ""
        out = []
        for name, d in sorted(self.defs.items()):
            a = self.alarms.get(name) or {"state": "INSUFFICIENT_DATA", "since": self.created, "reason": "Unchecked"}
            if want and name not in want or not name.startswith(prefix) or p.get("StateValue") not in (None,
                                                                                                         a["state"]):
                continue
            since = datetime.fromtimestamp(a["since"], UTC)
            out.append({**{k: v for k, v in d.items() if k != "Tags"},
                        "AlarmArn": f"arn:aws:cloudwatch:us-east-1:{ACCOUNT}:alarm:{name}",
                        "AlarmConfigurationUpdatedTimestamp": datetime.fromtimestamp(self.created, UTC),
                        "ActionsEnabled": d.get("ActionsEnabled", True), "StateValue": a["state"],
                        "StateReason": a["reason"], "StateUpdatedTimestamp": since,
                        "StateTransitionedTimestamp": since})
        return {"MetricAlarms": out, "CompositeAlarms": []}

    def cloudwatch(self, op: str, p: dict, now: float):
        if op == "GetMetricData":
            res = []
            for q in p.get("MetricDataQueries", []):
                ms = q.get("MetricStat") or {}
                met = ms.get("Metric") or {}
                if not met:
                    raise AwsError("ValidationError", "Only MetricStat queries are supported in this environment.", 400)
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
            stats = p.get("Statistics") or ["Average"]
            pts = self.aggregate(p["Namespace"], p["MetricName"], dims, p["StartTime"], p["EndTime"],
                                 p.get("Period", 60), stats, now)
            return {"Label": p["MetricName"], "Datapoints": [
                {"Timestamp": datetime.fromtimestamp(t, UTC), **{s: v[s] for s in stats}, "Unit": "None"}
                for t, v in pts]}
        if op == "ListMetrics":
            want = {d["Name"]: d.get("Value") for d in p.get("Dimensions") or []}
            return {"Metrics": [{"Namespace": ns, "MetricName": mn, "Dimensions": [{"Name": k, "Value": v}
                                                                                   for k, v in dims]}
                                for ns, mn, dims in self.series()
                                if p.get("Namespace") in (None, ns) and p.get("MetricName") in (None, mn)
                                and all(k in dict(dims) and v in (None, dict(dims)[k]) for k, v in want.items())]}
        if op == "DescribeAlarms":
            return self.describe_alarms(p, now)
        if op == "DescribeAlarmHistory":
            rows = [h for h in self.history if not p.get("AlarmName") or h["AlarmName"] == p["AlarmName"]]
            return {"AlarmHistoryItems": sorted(rows, key=lambda h: h["Timestamp"], reverse=True)[:100]}
        return None

    # ---- logs ----
    def lines(self, group: str, a: float, b: float) -> list[tuple[float, str, str]]:
        out = [x for x in self.stored.get(group, []) if a <= x[0] <= b]
        if group == "/ecs/payments-gateway":
            lo = max(a, D.ts("2026-11-14T00:00:00-05:00"))
            first = int(lo - lo % 60)
            for minute in range(first, int(min(b, self.m.now)), 60):
                for j in range(2):
                    mid = self.m.sets["established"][D.h("T04-bg", minute, j) % 3700]
                    out.append((minute + 11 + 23 * j, STREAMS[group], json.dumps(
                        {"level": "INFO", "event": "auth_rejected", "code": "ISSUER_DECLINE", "merchant": mid,
                         "detail": "Insufficient funds" if j else "Do Not Honor", "count_1m": 1},
                        separators=(",", ":"))))
                if minute >= D.ts("2026-11-21T11:00:00-05:00"):
                    out.append((minute + 41, STREAMS[group], json.dumps(
                        {"level": "INFO", "event": "auth_rejected", "code": "ISSUER_DECLINE", "merchant": "mch_15110",
                         "detail": "Do Not Honor", "count_1m": 6}, separators=(",", ":"))))
            out = [x for x in out if a <= x[0] <= b]
        return sorted(out)

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
            items = [{"logStreamName": st, "timestamp": _ms(t), "message": msg, "ingestionTime": _ms(t) + 410,
                      "eventId": f"{_ms(t)}{k:04d}"} for k, (t, st, msg) in enumerate(page, off)]
            if op == "GetLogEvents":
                return {"events": [{k: e[k] for k in ("timestamp", "message", "ingestionTime")} for e in items],
                        "nextForwardToken": f"f/{off + len(page)}", "nextBackwardToken": f"b/{max(0, off - PAGE)}"}
            more = off + len(page) < len(evs)
            return {"events": items, "searchedLogStreams": [], **({"nextToken": f"t/{off + len(page)}"} if more else {})}
        if op == "DescribeLogGroups":
            pre = p.get("logGroupNamePrefix") or ""
            return {"logGroups": [{"logGroupName": g, "creationTime": _ms(self.created), "metricFilterCount": 0,
                                   "arn": f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:{g}:*",
                                   "retentionInDays": 30 if g.startswith("/ecs") else 365,
                                   "storedBytes": 1_048_576 + 180 * len(self.stored.get(g, [])),
                                   "logGroupClass": "STANDARD"} for g in sorted(self.groups) if g.startswith(pre)]}
        if op == "DescribeLogStreams":
            g = self._known(p.get("logGroupName") or p.get("logGroupIdentifier"))
            names = sorted({x[1] for x in self.stored.get(g, [])} | ({STREAMS[g]} if g in STREAMS else set()))
            pre = p.get("logStreamNamePrefix") or ""
            rows = [{"logStreamName": n, "creationTime": _ms(self.created),
                     "arn": f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:{g}:log-stream:{n}",
                     "lastEventTimestamp": _ms(now - 30), "storedBytes": 0} for n in names if n.startswith(pre)]
            rows.sort(key=lambda r: r["logStreamName"], reverse=bool(p.get("descending")))
            return {"logStreams": rows[:int(p.get("limit") or 50)]}
        if op == "StartQuery":
            groups = [self._known(g) for g in p.get("logGroupNames") or p.get("logGroupIdentifiers") or
                      ([p["logGroupName"]] if p.get("logGroupName") else [])]
            if not groups:
                raise AwsError("InvalidParameterException", "Specify logGroupName, logGroupNames or "
                                                            "logGroupIdentifiers.", 400)
            ops = _xplan(p.get("queryString", ""))
            qid = f"{len(self.queries) + 1:08x}-7c2a-4e1d-9b3f-2a6d0e8c4b{len(self.queries) % 100:02d}"
            self.queries[qid] = self.run_query(p, groups, ops, now)
            return {"queryId": qid}
        if op == "GetQueryResults":
            if p.get("queryId") not in self.queries:
                raise AwsError("ResourceNotFoundException", "Query does not exist.", 400)
            return self.queries[p["queryId"]]
        return None

    def run_query(self, p: dict, groups: list[str], ops: list, now: float) -> dict:
        a, b = float(p.get("startTime") or now - 3600), min(float(p.get("endTime") or now), now)
        recs = []
        for g in groups:
            for t, stream, msg in self.lines(g, a, b):
                rec = {"@timestamp": _ms(t), "@message": msg, "@log": f"{ACCOUNT}:{g}", "@logStream": stream}
                if msg.startswith("{"):
                    try:
                        fields = json.loads(msg)
                    except ValueError:
                        fields = {}
                    rec.update({k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in fields.items()
                                if not k.startswith("@")})
                recs.append(rec)
        limit = int(p.get("limit") or 10000)
        results, matched = run_insights(ops, recs, limit, ["@timestamp", "@message"] + (["@log"] if len(groups) > 1
                                                                                        else []))
        return {"status": "Complete", "results": results,
                "statistics": {"recordsMatched": float(matched), "recordsScanned": float(len(recs)),
                               "bytesScanned": float(sum(len(r["@message"]) for r in recs))}}

    def serve(self, service: str, op: str, p: dict, now: float):
        if service in ("cloudwatch", "monitoring"):
            return self.cloudwatch(op, p, now)
        if service == "logs":
            return self.logs(op, p, now)
        return None

    def export(self, iso) -> dict:
        return {"alarm_state_timeline": [[iso(t), row] for t, row in self.timeline],
                "failures_by_minute": [[iso(t), n] for t, n in self.fail_series],
                "failures_by_merchant": [[iso(t), f] for t, f in zip(self.minutes, self.fails)
                                         if t >= self.m.t_start and f],
                "cell7_latency_ms_final": round(self.cell7, 1)}
